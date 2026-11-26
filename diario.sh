#!/bin/bash
# PolarMap · medición diaria de la prensa (sin redes sociales).
# Unos 40-60 minutos y alrededor de 2,3 $. Al terminar regenera la web
# y la publica (GitHub → Netlify).
#   ./diario.sh
set -euo pipefail
cd "$(dirname "$0")"
FECHA=$(date +%Y%m%d)
RUN="snapshot_ES_${FECHA}"
mkdir -p data/runs

echo "== Prensa (${RUN}) =="
caffeinate -i python3 pipeline_discover.py --countries ES --hours 24 \
  --out "${RUN}.json" 2>&1 | tee "data/runs/measure_ES_${FECHA}.log"

echo "== Web =="
python3 web/build_data.py

echo "== Publicar =="
./publicar_web.sh

echo "Listo. Medición ${RUN} guardada y web publicada."
