import gpxpy
import gpxpy.gpx
import time
import random
import json
import os
from threading import Thread
import pika

# --- Configuration (env-overridable) ---
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/%2F")
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")
RABBIT_ROUTING_KEY = os.getenv("RABBIT_ROUTING_KEY", "gps.update")
GPX_FILE_PATH = os.getenv("GPX_FILE_PATH", "trail_route.gpx")

ATHLETES = [
    {"name": "John Doe", "gender": "male"},
    {"name": "Jane Smith", "gender": "female"},
    {"name": "Alice Johnson", "gender": "female"},
    {"name": "Bob Brown", "gender": "male"},
]
SPEED_VARIATION = (6, 12)  # km/h


def get_channel():
    """
    Create a BlockingConnection + channel to RabbitMQ with exponential backoff.
    Returns (connection, channel). Declares the fanout exchange as durable.
    """
    params = pika.URLParameters(RABBIT_URL)
    backoff = 1
    while True:
        try:
            connection = pika.BlockingConnection(params)
            ch = connection.channel()
            ch.exchange_declare(exchange=RABBIT_EXCHANGE, exchange_type="fanout", durable=True)
            print("Simulator connected to RabbitMQ.")
            return connection, ch
        except Exception as e:
            print(f"RabbitMQ connect failed: {e}. Retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def read_gpx(file_path):
    with open(file_path, "r") as gpx_file:
        return gpxpy.parse(gpx_file)


def simulate_athlete(athlete, points):
    """
    Each athlete runs in its own thread and uses its own AMQP connection/channel.
    (pika BlockingConnection/Channel is NOT thread-safe.)
    """
    name = athlete["name"]
    gender = athlete["gender"]
    speed_kmh = random.uniform(*SPEED_VARIATION)
    speed_mps = max(speed_kmh / 3.6, 0.1)  # avoid division by zero

    conn, ch = get_channel()
    try:
        print(f"Simulating {name} ({gender}) at {speed_kmh:.2f} km/h")

        for i in range(len(points) - 1):
            start = points[i]
            end = points[i + 1]

            # Distance (m) and duration (s) for the segment
            distance = start.distance_3d(end) or 0.0
            duration = max(int(distance / speed_mps), 1)

            for t in range(duration):
                fraction = t / duration
                lat = start.latitude + fraction * (end.latitude - start.latitude)
                lon = start.longitude + fraction * (end.longitude - start.longitude)
                # Handle None elevations
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

                ch.basic_publish(
                    exchange=RABBIT_EXCHANGE,
                    routing_key=RABBIT_ROUTING_KEY,
                    body=json.dumps(event).encode("utf-8"),
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=2,  # persistent
                    ),
                )
                print(f"Published: {event}")
                time.sleep(1)
    finally:
        with conn:
            try:
                ch.close()
            except Exception:
                pass


def simulate_multiple_athletes():
    # Read the GPX once and flatten points
    gpx = read_gpx(GPX_FILE_PATH)
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)

    # Spawn one thread per athlete (each with its own AMQP connection)
    threads = []
    for athlete in ATHLETES:
        th = Thread(target=simulate_athlete, args=(athlete, points), daemon=True)
        threads.append(th)
        th.start()

    # Wait for all threads to finish (they finish when the route ends)
    for th in threads:
        th.join()


if __name__ == "__main__":
    simulate_multiple_athletes()