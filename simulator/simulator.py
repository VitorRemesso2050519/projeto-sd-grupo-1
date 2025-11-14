import gpxpy
import gpxpy.gpx
import time
import random
import json
import os
from threading import Thread
import pika

# --- Configuração (pode ser sobrescrita por variáveis de ambiente) ---
DEBUG = os.getenv("SIM_DEBUG", "0") == "1"  # Ativa modo de depuração se SIM_DEBUG=1
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/%2F")  # URL de ligação ao RabbitMQ
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")  # Exchange para publicação dos eventos
RABBIT_ROUTING_KEY = os.getenv("RABBIT_ROUTING_KEY", "gps.update")  # Routing key para eventos de GPS
GPX_FILE_PATH = os.getenv("GPX_FILE_PATH", "trail_route.gpx")  # Caminho do ficheiro GPX
PUBLISH_INTERVAL = float(os.getenv("SIM_PUBLISH_INTERVAL", "1"))  # Intervalo de publicação (segundos)

# Lista de atletas simulados
ATHLETES = [
    {"name": "John Doe", "gender": "male"},
    {"name": "Jane Smith", "gender": "female"},
    {"name": "Alice Johnson", "gender": "female"},
    {"name": "Bob Brown", "gender": "male"},
    {"name": "Michael Phelps", "gender": "male"},
]
SPEED_VARIATION = (6, 12)  # Variação da velocidade dos atletas (km/h)


def get_channel():
    """
    Cria uma ligação e canal ao RabbitMQ com backoff exponencial.
    Declara a exchange como durável. Retorna (connection, channel).
    """
    params = pika.URLParameters(RABBIT_URL)
    backoff = 1
    while True:
        try:
            connection = pika.BlockingConnection(params)
            ch = connection.channel()
            ch.exchange_declare(exchange=RABBIT_EXCHANGE, exchange_type="fanout", durable=True)
            if DEBUG:
                print("Simulador ligado ao RabbitMQ.")
            return connection, ch
        except Exception as e:
            print(f"Falha ao ligar ao RabbitMQ: {e}. A tentar novamente em {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def read_gpx(file_path):
    """
    Lê e faz o parsing do ficheiro GPX. Termina o programa se houver erro.
    """
    try:
        with open(file_path, "r") as gpx_file:
            return gpxpy.parse(gpx_file)
    except Exception as e:
        print(f"Erro ao ler o ficheiro GPX: {e}")
        exit(1)


def simulate_athlete(athlete, points):
    """
    Simula um atleta numa thread separada, cada um com ligação própria ao RabbitMQ.
    """
    name = athlete["name"]
    gender = athlete["gender"]
    speed_kmh = random.uniform(*SPEED_VARIATION)
    speed_mps = max(speed_kmh / 3.6, 0.1)  # Evita divisão por zero

    conn, ch = get_channel()
    try:
        if DEBUG:
            print(f"A simular {name} ({gender}) a {speed_kmh:.2f} km/h")

        for i in range(len(points) - 1):
            start = points[i]
            end = points[i + 1]

            # Calcula distância (m) e duração (s) do segmento
            distance = start.distance_3d(end) or 0.0
            duration = max(int(distance / speed_mps), 1)

            for t in range(duration):
                fraction = t / duration
                lat = start.latitude + fraction * (end.latitude - start.latitude)
                lon = start.longitude + fraction * (end.longitude - start.longitude)
                # Garante que a elevação não é None
                s_ele = start.elevation or 0.0
                e_ele = end.elevation or 0.0
                ele = s_ele + fraction * (e_ele - s_ele)

                event = {
                    "athlete": name,
                    "gender": gender,
                    "location": {"latitude": lat, "longitude": lon},
                    "elevation": ele,
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "event": "running",
                }

                try:
                    ch.basic_publish(
                        exchange=RABBIT_EXCHANGE,
                        routing_key=RABBIT_ROUTING_KEY,
                        body=json.dumps(event).encode("utf-8"),
                        properties=pika.BasicProperties(
                            content_type="application/json",
                            delivery_mode=2,  # Mensagem persistente
                        ),
                    )
                    if DEBUG:
                        print(f"Publicado: {event}")
                except Exception as pub_exc:
                    print(f"Erro ao publicar evento: {pub_exc}")
                time.sleep(PUBLISH_INTERVAL)
    finally:
        try:
            ch.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def simulate_multiple_athletes():
    """
    Lê o ficheiro GPX, extrai os pontos e inicia uma thread por atleta.
    """
    gpx = read_gpx(GPX_FILE_PATH)
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)

    threads = []
    for athlete in ATHLETES:
        th = Thread(target=simulate_athlete, args=(athlete, points), daemon=True)
        threads.append(th)
        th.start()

    for th in threads:
        th.join()


if __name__ == "__main__":
    simulate_multiple_athletes()