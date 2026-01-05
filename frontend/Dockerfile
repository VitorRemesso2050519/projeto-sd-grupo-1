# Usar a imagem base do Nginx
FROM nginx:alpine

RUN apk add --no-cache curl

# Copiar o arquivo HTML para o diretório padrão do Nginx
COPY . /usr/share/nginx/html/