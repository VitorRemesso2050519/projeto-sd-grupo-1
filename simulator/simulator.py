"""
Simulador de eventos de atletas para múltiplas corridas (GPX), publicando em RabbitMQ.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
import gpxpy
import time
import logging
import random
import json
import os
from pathlib import Path
from threading import Thread, Lock
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue, Empty
import pika
from prometheus_client import Counter, Gauge, start_http_server

###############################################################################
# CONFIGURAÇÃO E LOGGING
###############################################################################

# Ativa modo de depuração detalhado se SIM_DEBUG=1
DEBUG = os.getenv("SIM_DEBUG", "0") == "1"
# Pasta onde estão os ficheiros GPX das corridas
GPX_FOLDER = os.getenv("GPX_FOLDER", "./gpx-files")
# URL de ligação ao RabbitMQ
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://grupo1:a1s2d3f4g5h6@rabbitmq-cluster.rabbitmq-system.svc.cluster.local:5672/")
# Exchange para publicação dos eventos
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")
# Routing key para eventos de GPS
RABBIT_ROUTING_KEY = os.getenv("RABBIT_ROUTING_KEY", "gps.update")
# Intervalo entre publicações de eventos (em segundos)
PUBLISH_INTERVAL = float(os.getenv("SIM_PUBLISH_INTERVAL", "1"))
# Número de atletas a simular por corrida
NUM_ATHLETES = int(os.getenv("SIM_NUM_ATHLETES", "20"))


# --- Logging configurável ---

# Nível de logging configurável por variável de ambiente (DEBUG, INFO, etc)
LOG_LEVEL = os.getenv("SIM_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format='[%(asctime)s] %(levelname)s: %(message)s')
# Logger principal do simulador
logger = logging.getLogger("simulator")

###############################################################################
# HEALTHCHECK HTTP (para readiness/liveness probes)
###############################################################################
class HealthHandler(BaseHTTPRequestHandler):
    """Handler HTTP para healthcheck do simulador (usado em readiness/liveness probes)."""
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

def run_health_server():
    """Inicia um servidor HTTP para healthcheck na porta 8081."""
    server = HTTPServer(('0.0.0.0', 8081), HealthHandler)
    server.serve_forever()

# --- Métricas Prometheus ---
# Tráfego: Total de mensagens publicadas
# Contador Prometheus: total de mensagens publicadas no RabbitMQ, por atleta e corrida
messages_published_total = Counter(
    'simulator_messages_published_total',
    'Total de mensagens publicadas no RabbitMQ',
    ['athlete', 'race']
)

# Erros: Total de erros ao publicar
# Contador Prometheus: total de erros ao publicar mensagens
publish_errors_total = Counter(
    'simulator_publish_errors_total',
    'Total de erros ao publicar mensagens'
)

# Saturação: Atletas ativos
# Gauge Prometheus: número de atletas simulados ativos
active_athletes = Gauge(
    'simulator_active_athletes',
    'Número de atletas simulados ativos'
)

# Variação aleatória da velocidade dos atletas (em km/h)
SPEED_VARIATION = (6, 12)

###############################################################################
# GERAÇÃO DE ATLETAS
###############################################################################
def generate_random_name():
    """Gera um nome aleatório para atleta."""
    first_names = ["John", "Jane", "Alice", "Bob", "Michael", "Serena", "Usain", "Maria", "Carlos", "Ana", "David", "Laura", "Pedro", "Sofia", "Miguel", "Rita"]
    last_names = ["Doe", "Smith", "Johnson", "Brown", "Phelps", "Williams", "Bolt", "Silva", "Costa", "Martins", "Oliveira", "Santos", "Ferreira", "Gomes", "Alves", "Rocha"]
    return f"{random.choice(first_names)} {random.choice(last_names)}"

def generate_athletes(num, races):
    """Gera uma lista de atletas para cada corrida detectada."""
    genders = ["male", "female"]
    race_ids = list(races.keys())
    athletes = []
    if not race_ids:
        return athletes

    used_names = set()

    def get_unique_name():
        # Gera nomes até encontrar um que não foi usado
        while True:
            name = generate_random_name()
            if name not in used_names:
                used_names.add(name)
                return name

    # Para cada corrida, gera exatamente 'num' atletas
    for race_id in race_ids:
        for _ in range(num):
            athlete = {
                "name": get_unique_name(),
                "gender": random.choice(genders),
                "race": race_id
            }
            athletes.append(athlete)
    return athletes


# Lista global de atletas (gerada dinamicamente após descobrir as corridas)
ATHLETES = None

###############################################################################
# POOL DE CONEXÕES/CANAIS RABBITMQ
###############################################################################
# Tamanho do pool de conexões/canais RabbitMQ
POOL_SIZE = int(os.getenv("SIM_POOL_SIZE", "8"))
# Fila de conexões e canais para uso concorrente
_conn_pool = Queue(maxsize=POOL_SIZE)
_ch_pool = Queue(maxsize=POOL_SIZE)
# Lock para sincronização de acesso ao pool (não usado diretamente, mas útil para expansão)
_pool_lock = Lock()

def _init_rabbitmq_pool():
    """Inicializa o pool de conexões/canais para uso por múltiplas threads."""
    for _ in range(POOL_SIZE):
        conn = get_connection()
        ch = conn.channel()
        ch.exchange_declare(exchange=RABBIT_EXCHANGE, exchange_type='fanout', durable=True)
        _conn_pool.put(conn)
        _ch_pool.put(ch)
    logger.info(f"Pool de conexões/canais RabbitMQ inicializado com {POOL_SIZE} conexões.")

def get_pooled_channel():
    """Obtém uma conexão/canal do pool (bloqueia até estar disponível ou esgota)."""
    # Bloqueia indefinidamente até que um canal/conexão esteja disponível
    conn = _conn_pool.get()
    ch = _ch_pool.get()
    return conn, ch

def release_pooled_channel(conn, ch):
    """Devolve a conexão/canal ao pool após uso pela thread."""
    if conn and ch:
        _conn_pool.put(conn)
        _ch_pool.put(ch)

###############################################################################
# FUNÇÕES DE CONEXÃO RABBITMQ
###############################################################################
def get_connection():
    """Cria uma nova conexão RabbitMQ (usado internamente pelo pool)."""
    """Create a new connection to RabbitMQ."""
    parameters = pika.URLParameters(RABBIT_URL)
    return pika.BlockingConnection(parameters)

def get_channel():
    """Cria um novo canal RabbitMQ (usado apenas para inicialização do pool)."""
    """Create a channel on the shared connection."""
    conn = get_connection()
    channel = conn.channel()
    channel.exchange_declare(exchange=RABBIT_EXCHANGE, exchange_type='fanout', durable=True)
    return conn, channel

###############################################################################
# LEITURA DE GPX E DESCOBERTA DE CORRIDAS
###############################################################################
def read_gpx(file_path):
    """Lê e faz o parsing de um ficheiro GPX, retornando o objeto GPX ou None."""
    """
    Lê e faz o parsing do ficheiro GPX. Retorna None se houver erro.
    """
    try:
        with open(file_path, "r") as f:
            return gpxpy.parse(f)
    except Exception as e:
        logger.error(f"[GPX] Erro ao ler/parsing {file_path}: {e}")
        return None

def discover_races():
    """Descobre todos os ficheiros GPX na pasta configurada e retorna dict {race_id: file_path}."""
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

###############################################################################
# SIMULAÇÃO DE ATLETAS E PUBLICAÇÃO DE EVENTOS
###############################################################################
def simulate_athlete(race_id, athlete, points, batch_mode=False, batch_size=10):
    """Simula um atleta numa corrida, publicando eventos de localização para o RabbitMQ."""
    """
    Simula um atleta numa corrida específica.
    """
    name = athlete["name"]
    gender = athlete["gender"]
    speed_kmh = random.uniform(*SPEED_VARIATION)
    speed_mps = max(speed_kmh / 3.6, 0.1)

    conn = None
    ch = None
    active_athletes.inc()
    try:
        conn, ch = get_pooled_channel()
        if DEBUG:
            logger.info(f"[{race_id}] A simular {name} ({gender}) a {speed_kmh:.2f} km/h")
        batch = []
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
                batch.append(event)
                if batch_mode and len(batch) >= batch_size:
                    _publish_batch(ch, batch, name, race_id)
                    batch.clear()
                elif not batch_mode:
                    _publish_event(ch, event, name, race_id)
                time.sleep(PUBLISH_INTERVAL)
        if batch_mode and batch:
            _publish_batch(ch, batch, name, race_id)
    except Exception as e:
        logger.error(f"[{race_id}] Erro na simulação do atleta {name}: {e}")
    finally:
        active_athletes.dec()
        release_pooled_channel(conn, ch)
def _publish_event(ch, event, name, race_id):
    """Publica um único evento no RabbitMQ."""
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
        logger.info(f"[PUB] Corrida: {race_id} | Atleta: {name} | Localização: {event.get('location')} | Evento: {event.get('event')}")
        if DEBUG:
            logger.debug(f"[{race_id}] Publicado: {event}")
    except pika.exceptions.AMQPError as pub_exc:
        publish_errors_total.inc()
        logger.error(f"[{race_id}] Erro ao publicar evento: {pub_exc}")

def _publish_batch(ch, batch, name, race_id):
    """Publica um lote de eventos no RabbitMQ."""
    for event in batch:
        logger.info(f"[BATCH PUB] Corrida: {race_id} | Atleta: {name} | Localização: {event.get('location')} | Evento: {event.get('event')}")
        _publish_event(ch, event, name, race_id)

def simulate_race(race_id, gpx_file, batch_mode=False, batch_size=10):
    """Executa a simulação de uma corrida, criando threads para cada atleta."""
    """
    Run a single race: read points, start threads only for athletes in this race.
    """
    logger.info(f"[SIM] Iniciando simulação da corrida {race_id} com ficheiro {gpx_file}")
    gpx = read_gpx(gpx_file)
    if not gpx:
        logger.error(f"[{race_id}] GPX inválido ou erro ao ler/parsing: {gpx_file}")
        return
    # Cache de pontos GPX
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)
    logger.info(f"[SIM] Corrida {race_id}: {len(points)} pontos GPX carregados")
    if not points:
        logger.error(f"[{race_id}] Nenhum ponto encontrado no GPX: {gpx_file}")
        return
    # Permitir que todos os atletas sejam simulados, cada thread aguarda canal disponível
    atletas_corrida = [a for a in ATHLETES if a.get("race") == race_id]
    num_atletas = len(atletas_corrida)
    logger.info(f"[SIM] Corrida {race_id}: {num_atletas} atletas a simular: {[a['name'] for a in atletas_corrida]}")
    if num_atletas == 0:
        logger.warning(f"[SIM] Corrida {race_id}: Nenhum atleta encontrado para simular!")
        return
    with ThreadPoolExecutor(max_workers=num_atletas) as executor:
        futures = []
        for athlete in atletas_corrida:
            futures.append(executor.submit(simulate_athlete, race_id, athlete, points, batch_mode, batch_size))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Erro em atleta da corrida {race_id}: {e}")
    logger.info(f"[{race_id}] Corrida concluída")


###############################################################################
# EXECUÇÃO PRINCIPAL DA SIMULAÇÃO
###############################################################################
def simulate_all_races(batch_mode=False, batch_size=10):
    """Descobre corridas, gera atletas e executa simulação para todas as corridas em paralelo."""
    """
    Descobre todas as corridas e executa cada uma em paralelo.
    """
    global ATHLETES
    races = discover_races()
    if not races:
        print("Nenhuma corrida encontrada.")
        return

    logger.info(f"Corridas descobertas: {list(races.keys())}")

    # Gerar atletas dinamicamente
    global ATHLETES
    ATHLETES = generate_athletes(NUM_ATHLETES, races)
    logger.info(f"Total de atletas gerados: {len(ATHLETES)}")
    for race_id in races.keys():
        atletas_corrida = [a['name'] for a in ATHLETES if a['race'] == race_id]
        logger.info(f"Atletas para corrida {race_id}: {atletas_corrida}")

    # Inicializar pool de conexões/canais
    _init_rabbitmq_pool()
    # Cache de pontos GPX por corrida
    with ThreadPoolExecutor(max_workers=min(4, len(races))) as executor:
        futures = []
        for race_id, gpx_path in races.items():
            futures.append(executor.submit(simulate_race, race_id, gpx_path, batch_mode, batch_size))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Erro na corrida: {e}")

###############################################################################
# ENTRADA PRINCIPAL
###############################################################################
if __name__ == "__main__":
    # Inicia Prometheus na porta 8080
    start_http_server(8080)
    logger.info("Servidor de métricas Prometheus iniciado na porta 8080")
    # Inicia o endpoint /health na porta 8081
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()
    logger.info("Endpoint de health iniciado na porta 8081 (/health)")
    # Parâmetros de batch podem ser ajustados por env
    batch_mode = os.getenv("SIM_BATCH_MODE", "0") == "1"
    batch_size = int(os.getenv("SIM_BATCH_SIZE", "10"))
    simulate_all_races(batch_mode=batch_mode, batch_size=batch_size)
    # Mantém o processo vivo para scraping de métricas e health
    while True:
        time.sleep(60)