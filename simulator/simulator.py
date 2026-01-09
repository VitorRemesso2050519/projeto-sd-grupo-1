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
RABBIT_URL = os.getenv("RABBIT_URL", "")
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
    first_names = [
        "John", "Jane", "Alice", "Bob", "Michael", "Serena", "Usain", "Maria",
        "Carlos", "Ana", "David", "Laura", "Pedro", "Sofia", "Miguel", "Rita",
        "Tiago", "Beatriz", "João", "Patrícia", "André", "Carolina", "Ricardo", "Inês",
        "Francisco", "Marta", "Bruno", "Helena", "Diogo", "Catarina", "Guilherme", "Raquel"
    ]
    last_names = [
        "Doe", "Smith", "Johnson", "Brown", "Phelps", "Williams", "Bolt", "Silva",
        "Costa", "Martins", "Oliveira", "Santos", "Ferreira", "Gomes", "Alves", "Rocha",
        "Sousa", "Barros", "Pereira", "Mendes", "Lopes", "Ramos", "Teixeira", "Correia",
        "Monteiro", "Faria", "Henriques", "Cunha", "Neves", "Fonseca", "Moura", "Vasconcelos"
    ]
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
    name = athlete["name"]
    gender = athlete["gender"]
    speed_kmh = random.uniform(*SPEED_VARIATION)
    speed_mps = max(speed_kmh / 3.6, 0.1)

    active_athletes.inc()
    try:
        if DEBUG:
            logger.info(f"[{race_id}] A simular {name} ({gender}) a {speed_kmh:.2f} km/h")
        # ENVIO DE EVENTO MAL FORMATADO PARA TESTE
        conn, ch = get_pooled_channel()
        try:
            # Evento JSON inválido (sintaxe)
            malformed_event = "{race_id: 'MALFORMED', athlete: '???', location: [0,0], event: running"  # JSON inválido
            ch.basic_publish(
                exchange=RABBIT_EXCHANGE,
                routing_key=RABBIT_ROUTING_KEY,
                body=malformed_event.encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                ),
            )
            logger.info(f"[PUB-TESTE] Evento mal formatado enviado para {race_id}")

            # Evento JSON válido, mas faltando campo obrigatório (race_id)
            incomplete_event = {
                "athlete": name,
                "gender": gender,
                "location": {"latitude": 0, "longitude": 0},
                "elevation": 0,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": "running"
            }
            ch.basic_publish(
                exchange=RABBIT_EXCHANGE,
                routing_key=RABBIT_ROUTING_KEY,
                body=json.dumps(incomplete_event).encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                ),
            )
            logger.info(f"[PUB-TESTE] Evento incompleto (sem race_id) enviado para {race_id}")
        finally:
            release_pooled_channel(conn, ch)
        # FIM DO ENVIO DE EVENTO MAL FORMATADO
        for i in range(len(points) - 1):
            try:
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
                    conn, ch = get_pooled_channel()
                    try:
                        _publish_event(ch, event, name, race_id)
                    finally:
                        release_pooled_channel(conn, ch)
                    time.sleep(PUBLISH_INTERVAL)
            except Exception as loop_exc:
                logger.error(f"[{race_id}] Erro no ponto {i} do atleta {name}: {loop_exc}")
    except Exception as e:
        logger.error(f"[{race_id}] Erro na simulação do atleta {name}: {e}")
    finally:
        active_athletes.dec()
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



def simulate_race(race_id, gpx_file):
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
            futures.append(executor.submit(simulate_athlete, race_id, athlete, points))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Erro em atleta da corrida {race_id}: {e}")
    logger.info(f"[{race_id}] Corrida concluída")


###############################################################################
# EXECUÇÃO PRINCIPAL DA SIMULAÇÃO
###############################################################################
def simulate_all_races():
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
    # Carregar pontos GPX de todas as corridas
    gpx_points_by_race = {}
    for race_id, gpx_path in races.items():
        gpx = read_gpx(gpx_path)
        if not gpx:
            logger.error(f"[{race_id}] GPX inválido ou erro ao ler/parsing: {gpx_path}")
            continue
        points = []
        for track in gpx.tracks:
            for segment in track.segments:
                points.extend(segment.points)
        if not points:
            logger.error(f"[{race_id}] Nenhum ponto encontrado no GPX: {gpx_path}")
            continue
        gpx_points_by_race[race_id] = points
        logger.info(f"[SIM] Corrida {race_id}: {len(points)} pontos GPX carregados (pré-cache)")

    # Submeter todos os atletas de todas as corridas para execução paralela
    all_futures = []
    # Permitir simulação de todos os atletas em paralelo
    with ThreadPoolExecutor(max_workers=len(ATHLETES)) as executor:
        for athlete in ATHLETES:
            race_id = athlete['race']
            points = gpx_points_by_race.get(race_id)
            if not points:
                logger.warning(f"[SIM] Atleta {athlete['name']} ignorado: corrida {race_id} sem pontos carregados.")
                continue
            all_futures.append(executor.submit(simulate_athlete, race_id, athlete, points))
        for future in as_completed(all_futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Erro na simulação de atleta: {e}")

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
    simulate_all_races()
    # Mantém o processo vivo para scraping de métricas e health
    while True:
        time.sleep(60)