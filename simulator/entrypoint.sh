#!/bin/sh

cp -rn /tmp/gpx-files/* /app/gpx-files/
echo "GPX files copied to volume."
exec python simulator.py
