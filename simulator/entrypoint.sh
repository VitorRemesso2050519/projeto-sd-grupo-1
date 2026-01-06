#!/bin/sh


# Copia apenas ficheiros que ainda não existem no destino
for src in /tmp/gpx-files/*; do
	dest="/app/gpx-files/$(basename "$src")"
	if [ ! -e "$dest" ]; then
		cp "$src" "$dest"
	fi
done
echo "GPX files copied to volume (only new files)."
exec python simulator.py
