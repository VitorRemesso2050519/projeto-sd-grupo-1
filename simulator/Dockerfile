FROM python:3.9-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install gpxpy pika prometheus-client

WORKDIR /app
COPY simulator.py /app/simulator.py
COPY gpx-files/ /tmp/gpx-files/
COPY entrypoint.sh /app/entrypoint.sh
RUN mkdir -p /app/gpx-files /app/data && chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
