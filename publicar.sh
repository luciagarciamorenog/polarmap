#!/bin/bash
# PolarMap · publicar cualquier cambio (código, prompts, web, README o datos).
#   ./publicar.sh "qué has cambiado"
# 1) pasa los tests, 2) regenera la web, 3) comprueba que no se cuela
# ninguna clave, 4) guarda el cambio en git y lo sube a GitHub.
# Netlify está conectado a GitHub y publica https://polarmap.netlify.app solo.
# Si algo falla, no se publica nada.
set -uo pipefail
cd "$(dirname "$0")"

MSG="${1:-}"
if [ -z "$MSG" ]; then
  echo 'Uso: ./publicar.sh "qué has cambiado"'
  exit 1
fi

echo "== 1/4 Tests =="
TESTS="analysis/test_metrics.py analysis/test_outlets.py analysis/test_discover.py
analysis/test_contract.py analysis/test_runner.py analysis/test_ecosystem.py
ingest/test_gkg.py ingest/test_leads.py validate/pilot/test_stats.py
analysis/test_common_ground.py"
for t in $TESTS; do
  if ! python3 "$t" > /tmp/polarmap_test.log 2>&1; then
    echo "   Falla $t:"; tail -15 /tmp/polarmap_test.log
    echo "No se ha publicado nada."; exit 1
  fi
done
echo "   todos pasan"

echo "== 2/4 Web =="
if ! python3 web/build_data.py | tail -1; then
  echo "No se pudo generar la web. No se ha publicado nada."; exit 1
fi

echo "== 3/4 Seguridad =="
git add -A
if git diff --cached --name-only | grep -qE '(^|/)\.env$'; then
  git reset -q; echo "   El fichero .env iba a subirse. No se ha publicado nada."; exit 1
fi
if git diff --cached | grep -qE 'sk-ant-[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}'; then
  git reset -q; echo "   Hay algo que parece una clave en los cambios. No se ha publicado nada."; exit 1
fi
echo "   sin claves"

echo "== 4/4 Publicar =="
if git diff --cached --quiet; then
  echo "   no hay cambios nuevos que guardar"
else
  git commit -q -m "$MSG" && echo "   guardado: $MSG"
fi
if git push -q origin main; then
  echo "Listo. GitHub actualizado; la web se publica en 1-2 minutos: https://polarmap.netlify.app"
else
  echo "No se pudo subir a GitHub (¿sin conexión?). El cambio está guardado; repite: ./publicar.sh \"$MSG\""
fi
