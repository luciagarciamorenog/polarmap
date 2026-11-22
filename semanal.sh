#!/bin/bash
# PolarMap · tirada semanal (opción A: foto de 24 h, un día fijo a la semana).
# Prensa y, con sus mismos debates y ventana, Bluesky y YouTube.
#   ./semanal.sh
set -euo pipefail
cd "$(dirname "$0")"
FECHA=$(date +%Y%m%d)
RUN="snapshot_ES_${FECHA}"
mkdir -p data/runs

echo "== Prensa (${RUN}) =="
caffeinate -i python3 pipeline_discover.py --countries ES --hours 24 \
  --out "${RUN}.json" 2>&1 | tee "data/runs/measure_ES_${FECHA}.log"

echo "== Redes sociales =="
caffeinate -i python3 pipeline_social.py --press-run "${RUN}" \
  2>&1 | tee "data/runs/social_ES_${FECHA}.log"

echo "Listo. Informes en data/internal/${RUN}_ecosystem.json y ${RUN}_social_report.json"
