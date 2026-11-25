#!/bin/bash
# PolarMap · publica la web y los resultados de la última corrida.
# Sube a GitHub web/ y data/ (lo que .gitignore deja: sin caché, sin
# titulares sueltos ni entradillas). Netlify está conectado al repositorio
# y publica https://polarmap.netlify.app solo, en 1-2 minutos.
#   ./publicar_web.sh        (diario.sh lo llama al terminar)
set -uo pipefail
cd "$(dirname "$0")"

git add web data
if git diff --cached --quiet; then
  echo "Nada nuevo que publicar."
  exit 0
fi
git commit -q -m "Corrida $(date +%Y-%m-%d): resultados y web"
if git push -q origin main; then
  echo "Subido a GitHub. La web se actualizará en 1-2 minutos: https://polarmap.netlify.app"
else
  echo "No se pudo subir a GitHub (¿sin conexión o sin token?). Los datos están guardados;"
  echo "vuelve a intentarlo con: ./publicar_web.sh"
fi
