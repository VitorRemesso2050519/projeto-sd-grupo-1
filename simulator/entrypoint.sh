#!/bin/sh

# Apaga todos os ficheiros existentes na pasta de destino
rm -rf /app/gpx-files/*
# Copia sempre os ficheiros da origem para o destino
cp -r /tmp/gpx-files/* /app/gpx-files/
echo "GPX files copied to volume."
exec python simulator.py
