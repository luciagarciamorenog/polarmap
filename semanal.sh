#!/bin/bash
# PolarMap · tirada semanal (opción A: foto de 24 h, cada lunes:
# noticias y redes del domingo al lunes).
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
# Sin la capa «oficial» de momento: PSOE, PP y Vox no publican canal RSS y
# la capa mediría solo a una parte del arco político (24-nov-2026).
caffeinate -i python3 pipeline_social.py --press-run "${RUN}" \
  --sources bluesky,replies,youtube \
  2>&1 | tee "data/runs/social_ES_${FECHA}.log"

echo "== Web =="
python3 web/build_data.py

echo "Listo. Web en web/index.html. Informes en data/internal/${RUN}_ecosystem.json y ${RUN}_social_report.json"
