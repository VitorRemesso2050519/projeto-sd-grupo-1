# Usar a imagem base do Python
FROM python:3.9-slim

# Instalar dependências
RUN pip install requests gpxpy pika prometheus-client


# Copiar o código do simulador e ficheiro GPX para o local correto
WORKDIR /app
COPY simulator.py /app/simulator.py
RUN mkdir -p /app/gpx-files /app/data

EXPOSE 8080

# Comando para iniciar o simulador
CMD ["python", "simulator.py"]