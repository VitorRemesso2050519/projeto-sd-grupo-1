# Usar a imagem base do Python
FROM python:3.9-slim

# Instalar dependências
RUN pip install requests gpxpy pika


# Copiar o código do simulador e ficheiro GPX para o local correto
WORKDIR /app
COPY simulator.py /app/simulator.py
RUN mkdir -p /app/data
COPY trail_route.gpx /app/data/trail_route.gpx

# Copiar o arquivo HTML para o diretório padrão do Nginx
COPY trail_route.gpx /usr/share/nginx/html/gpx

# Comando para iniciar o simulador
CMD ["python", "simulator.py"]