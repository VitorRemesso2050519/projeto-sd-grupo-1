from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import contextlib
import json
import os
from typing import List
import aio_pika

# --- Configuração (pode ser sobrescrita por variáveis de ambiente) ---
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")  # URL de ligação ao RabbitMQ
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")  # Exchange para publicação dos eventos
RABBIT_QUEUE = os.getenv("RABBIT_QUEUE", "events.gps")  # Fila para eventos de GPS

# Instancia a aplicação FastAPI
app = FastAPI(title="Trail Backend (WS + RabbitMQ)")

# Permitir CORS para o frontend (em produção, restringir aos domínios necessários)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção, substituir por lista de domínios permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ligações WebSocket ativas (apenas para instância atual)
connections: List[WebSocket] = []


@app.get("/health")
async def health():
    """Endpoint para healthcheck do serviço."""
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Endpoint WebSocket para comunicação em tempo real com clientes."""
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            # Mantém o socket ativo
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Erro inesperado na ligação WebSocket: {e}")
    finally:
        with contextlib.suppress(ValueError):
            connections.remove(websocket)
        print("Cliente WebSocket desconectado")


@app.post("/events")
async def receive_event(event: dict):
    """
    Endpoint HTTP opcional para compatibilidade retroativa.
    Difunde o evento recebido para todos os clientes WebSocket conectados.
    """
    # Validação do evento
    required_fields = {"athlete", "gender", "location", "elevation", "time", "event"}
    if not isinstance(event, dict) or not required_fields.issubset(event.keys()):
        return {"status": "erro", "detail": "Formato de evento inválido. Campos obrigatórios: " + ', '.join(required_fields)}
    await _broadcast(event)
    return {"status": "evento enviado"}


async def _broadcast(event: dict):
    """Envia um evento para todos os clientes WebSocket conectados."""
    disconnected = []
    for ws in list(connections):
        try:
            await ws.send_json(event)
        except Exception as e:
            print(f"Erro ao enviar para WebSocket: {e}")
            disconnected.append(ws)
    for ws in disconnected:
        with contextlib.suppress(ValueError):
            connections.remove(ws)


# -----------------------------
# Consumidor resiliente RabbitMQ
# -----------------------------
async def _consume_loop():
    """
    Loop de fundo robusto que tenta ligar ao RabbitMQ,
    declara exchange/fila/binding e transmite mensagens para _broadcast.
    Usa backoff exponencial em falhas e recupera de reinícios do broker.
    """
    backoff = 1
    while True:
        try:
            # Ligação robusta (lida automaticamente com algumas falhas)
            conn = await aio_pika.connect_robust(RABBIT_URL)
            try:
                channel = await conn.channel()
                exchange = await channel.declare_exchange(
                    RABBIT_EXCHANGE,
                    aio_pika.ExchangeType.FANOUT,
                    durable=True,
                )
                queue = await channel.declare_queue(RABBIT_QUEUE, durable=True)
                await queue.bind(exchange)

                # Reset ao backoff após ligação bem-sucedida
                backoff = 1
                print("Consumidor RabbitMQ ligado e pronto.")

                async with queue.iterator() as q:
                    async for message in q:
                        async with message.process():
                            try:
                                event = json.loads(message.body)
                                await _broadcast(event)
                            except Exception as e:
                                print(f"Mensagem inválida: {e}")
            finally:
                # Fecha a ligação se sair do loop de consumo
                with contextlib.suppress(Exception):
                    await conn.close()

        except Exception as e:
            # Falha de ligação ou canal; tenta novamente com backoff
            print(f"Falha ao ligar/consumir RabbitMQ: {e}. A tentar novamente em {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)  # máximo 30s


@app.on_event("startup")
async def start_bg_consumer():
    """Inicia o consumidor RabbitMQ em background ao arrancar o serviço."""
    app.state.consumer_task = asyncio.create_task(_consume_loop())


@app.on_event("shutdown")
async def stop_bg_consumer():
    """Cancela o consumidor RabbitMQ e fecha todas as ligações WebSocket ao terminar o serviço."""
    task = getattr(app.state, "consumer_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(Exception):
            await task
    # Fecha todas as ligações WebSocket
    for ws in list(connections):
        with contextlib.suppress(Exception):
            await ws.close()
    connections.clear()

# Documentação para integração do frontend:
# O frontend deve ligar-se ao endpoint WebSocket do backend em ws://<host>:8000/ws
# Os eventos recebidos são enviados em formato JSON e contêm os campos:
# athlete, gender, location, elevation, time, event
# O frontend deve estar preparado para tentar reconectar em caso de falha na ligação WebSocket.