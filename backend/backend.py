from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
import asyncio
import contextlib
import json
import os
from typing import List
import aio_pika
import time

# --- Configuração (pode ser sobrescrita por variáveis de ambiente) ---
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://grupo1:a1s2d3f4g5h6@rabbitmq-cluster.rabbitmq-system.svc.cluster.local:5672/grupo1/")  # URL de ligação ao RabbitMQ
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")  # Exchange para publicação dos eventos
RABBIT_QUEUE = os.getenv("RABBIT_QUEUE", "events.gps")  # Fila para eventos de GPS

# Instancia a aplicação FastAPI
app = FastAPI(title="Trail Backend (WS + RabbitMQ)")

# --- Métricas Prometheus ---
# Tráfego: Total de requisições HTTP
http_requests_total = Counter(
    'http_requests_total',
    'Total de requisições HTTP',
    ['method', 'endpoint', 'status']
)

# Latência: Duração das requisições HTTP
http_request_duration_seconds = Histogram(
    'http_request_duration_seconds',
    'Duração das requisições HTTP em segundos',
    ['method', 'endpoint']
)

# Erros: Total de erros
http_errors_total = Counter(
    'http_errors_total',
    'Total de erros HTTP',
    ['method', 'endpoint', 'status']
)

# Saturação: Conexões WebSocket ativas
websocket_connections_active = Gauge(
    'websocket_connections_active',
    'Número de conexões WebSocket ativas'
)

# Tráfego RabbitMQ: Mensagens consumidas
rabbitmq_messages_consumed_total = Counter(
    'rabbitmq_messages_consumed_total',
    'Total de mensagens consumidas do RabbitMQ'
)

# Erros RabbitMQ
rabbitmq_errors_total = Counter(
    'rabbitmq_errors_total',
    'Total de erros ao consumir mensagens do RabbitMQ'
)

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

# Middleware para medir latência e tráfego
@app.middleware("http")
async def metrics_middleware(request, call_next):
    start_time = time.time()
    method = request.method
    endpoint = request.url.path
    
    try:
        response = await call_next(request)
        status = response.status_code
        
        # Registar tráfego
        http_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
        
        # Registar erros (status >= 400)
        if status >= 400:
            http_errors_total.labels(method=method, endpoint=endpoint, status=status).inc()
        
        # Registar latência
        duration = time.time() - start_time
        http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)
        
        return response
    except Exception as e:
        # Registar erros de exceção
        http_errors_total.labels(method=method, endpoint=endpoint, status=500).inc()
        raise

@app.get("/metrics")
async def metrics():
    """Endpoint para expor métricas Prometheus"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    """Endpoint para healthcheck do serviço."""
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Endpoint WebSocket para comunicação em tempo real com clientes."""
    await websocket.accept()
    connections.append(websocket)
    websocket_connections_active.set(len(connections))  # Atualizar saturação
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
        websocket_connections_active.set(len(connections))  # Atualizar saturação
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
    websocket_connections_active.set(len(connections))


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
                                body = message.body.decode("utf-8")
                                event = json.loads(body)
                                rabbitmq_messages_consumed_total.inc()  # Registar tráfego
                                await _broadcast(event)
                            except Exception as e:
                                rabbitmq_errors_total.inc()  # Registar erro
                                print(f"Erro ao processar mensagem: {e}")
            finally:
                # Fecha a ligação se sair do loop de consumo
                with contextlib.suppress(Exception):
                    await conn.close()

        except Exception as e:
            # Falha de ligação ou canal; tenta novamente com backoff
            rabbitmq_errors_total.inc()  # Registar erro de conexão
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
