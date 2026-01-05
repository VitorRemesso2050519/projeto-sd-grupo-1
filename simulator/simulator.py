import gpxpy
import time
import random
import json
import os
from pathlib import Path
from threading import Thread
import pika
from prometheus_client import Counter, Gauge, start_http_server

# --- Configuração (pode ser sobrescrita por variáveis de ambiente) ---
DEBUG = os.getenv("SIM_DEBUG", "0") == "1"  # Ativa modo de depuração se SIM_DEBUG=1
GPX_FOLDER = os.getenv("GPX_FOLDER", "./gpx-files")  # Caminho dos ficheiros GPX
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://grupo1:a1s2d3f4g5h6@rabbitmq-cluster.rabbitmq-system.svc.cluster.local:5672/")  # URL de ligação ao RabbitMQ
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")  # Exchange para publicação dos eventos
RABBIT_ROUTING_KEY = os.getenv("RABBIT_ROUTING_KEY", "gps.update")  # Routing key para eventos de GPS
PUBLISH_INTERVAL = float(os.getenv("SIM_PUBLISH_INTERVAL", "1"))  # Intervalo de publicação (segundos)

# --- Métricas Prometheus ---
# Tráfego: Total de mensagens publicadas
messages_published_total = Counter(
    'simulator_messages_published_total',
    'Total de mensagens publicadas no RabbitMQ',
    ['athlete', 'race']
)

# Erros: Total de erros ao publicar
publish_errors_total = Counter(
    'simulator_publish_errors_total',
    'Total de erros ao publicar mensagens'
)

# Saturação: Atletas ativos
active_athletes = Gauge(
    'simulator_active_athletes',
    'Número de atletas simulados ativos'
)

# Lista de atletas simulados
ATHLETES = [
    {"name": "John Doe", "gender": "male", "race": "trail_route1"},
    {"name": "Jane Smith", "gender": "female", "race": "trail_route1"},
    {"name": "Alice Johnson", "gender": "female", "race": "trail_route2"},
    {"name": "Bob Brown", "gender": "male", "race": "trail_route1"},
    {"name": "Michael Phelps", "gender": "male", "race": "trail_route2"},
    {"name": "Serena Williams", "gender": "female", "race": "trail_route2"},
    {"name": "Usain Bolt", "gender": "male", "race": "trail_route1"},
]
SPEED_VARIATION = (6, 12)  # Variação da velocidade dos atletas (km/h)

# Global connection (create once)
_connection = None
_connection_lock = threading.Lock()

def get_connection():
    global _connection
    with _connection_lock:
        if _connection is None or _connection.is_closed:
            _connection = pika.BlockingConnection(pika.URLParameters(RABBIT_URL))
        return _connection

def get_channel():
    """Create a channel on the shared connection."""
    conn = get_connection()
    channel = conn.channel()
    channel.exchange_declare(exchange=RABBIT_EXCHANGE, exchange_type='fanout', durable=True)
    return conn, channel

def read_gpx(file_path):
    """
    Lê e faz o parsing do ficheiro GPX. Retorna None se houver erro.
    """
    try:
        with open(file_path, "r") as f:
            return gpxpy.parse(f)
    except Exception as e:
        print(f"Erro ao ler {file_path}: {e}")
        return None

def discover_races():
    """
    Discover all GPX files in the GPX folder.
    Returns a dict: {race_id: file_path}
    """
    gpx_folder = Path(GPX_FOLDER)
    races = {}
    
    if not gpx_folder.exists():
        print(f"Pasta GPX não encontrada: {gpx_folder}")
        return races
    
    for gpx_file in gpx_folder.glob("*.gpx"):
        race_id = gpx_file.stem  # filename without extension
        races[race_id] = str(gpx_file)
    
    print(f"Corridas descobertas: {list(races.keys())}")
    return races

def simulate_athlete(race_id, athlete, points):
    """
    Simula um atleta numa corrida específica.
    """
    name = athlete["name"]
    gender = athlete["gender"]
    speed_kmh = random.uniform(*SPEED_VARIATION)
    speed_mps = max(speed_kmh / 3.6, 0.1)

    conn, ch = get_channel()
    active_athletes.inc()
    try:
        if DEBUG:
            print(f"[{race_id}] A simular {name} ({gender}) a {speed_kmh:.2f} km/h")

        for i in range(len(points) - 1):
            start = points[i]
            end = points[i + 1]

            distance = start.distance_3d(end) or 0.0
            duration = max(int(distance / speed_mps), 1)

            for t in range(duration + 1):
                fraction = t / duration
                lat = (start.latitude or 0.0) + fraction * ((end.latitude or 0.0) - (start.latitude or 0.0))
                lon = (start.longitude or 0.0) + fraction * ((end.longitude or 0.0) - (start.longitude or 0.0))
                s_ele = start.elevation or 0.0
                e_ele = end.elevation or 0.0
                ele = s_ele + fraction * (e_ele - s_ele)

                event = {
                    "race_id": race_id,
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
                            delivery_mode=2,
                        ),
                    )
                    messages_published_total.labels(athlete=name, race=race_id).inc()
                    if DEBUG:
                        print(f"[{race_id}] Publicado: {event}")
                except Exception as pub_exc:
                    publish_errors_total.inc()
                    print(f"[{race_id}] Erro ao publicar evento: {pub_exc}")
                    # Attempt to reconnect
                    try:
                        conn.close()
                    except:
                        pass
                    try:
                        conn, ch = get_channel()
                    except Exception as reconn_exc:
                        print(f"[{race_id}] Falha ao reconectar: {reconn_exc}")
                        return  # Exit thread gracefully
                time.sleep(PUBLISH_INTERVAL)
    finally:
        active_athletes.dec()
        try:
            ch.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

def simulate_race(race_id, gpx_file):
    """
    Run a single race: read points, start threads only for athletes in this race.
    """
    gpx = read_gpx(gpx_file)
    if not gpx:
        print(f"[{race_id}] GPX inválido: {gpx_file}")
        return

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)
    if not points:
        print(f"[{race_id}] Nenhum ponto encontrado")
        return

    threads = []
    for athlete in ATHLETES:
        if athlete.get("race") != race_id:
            continue
        th = Thread(target=simulate_athlete, args=(race_id, athlete, points), daemon=True)
        threads.append(th)
        th.start()

    for th in threads:
        th.join()
    print(f"[{race_id}] Corrida concluída")

def simulate_all_races():
    """
    Discover all races in GPX_FOLDER and execute each one in parallel.
    """
    races = discover_races()
    if not races:
        print("Nenhuma corrida encontrada.")
        return

    race_threads = []
    for race_id, gpx_path in races.items():
        t = Thread(target=simulate_race, args=(race_id, gpx_path), daemon=True)
        race_threads.append(t)
        t.start()

    for t in race_threads:
        t.join()

if __name__ == "__main__":
    start_http_server(8080)
    print("Servidor de métricas Prometheus iniciado na porta 8080")
    simulate_all_races()
    # Keep the process alive for metrics scraping
    while True:
        time.sleep(60)