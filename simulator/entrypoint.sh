#!/bin/sh
if [ "$(ls -A /app/gpx-files)" ]; then
  echo "GPX folder not empty, skipping copy."
else
  cp -r /tmp/gpx-files/* /app/gpx-files/
  echo "GPX files copied to volume."
fi
exec python simulator.py
