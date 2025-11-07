# Usar a imagem base do Python
FROM python:3.9-slim

# Instalar dependências
RUN pip install fastapi uvicorn[standard] aio-pika

# Copiar o código do backend
WORKDIR /app
COPY backend.py /app/backend.py

# Expor a porta 8000
EXPOSE 8000

# Comando para iniciar o backend
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]