# Plan B: ficheros GKG (sin API, sin bloqueos)

## Qué cambia

En vez de PEDIR datos a la API de GDELT (que te bloquea), DESCARGAS sus
ficheros publicados. Nadie puede bloquearte: es una descarga normal.

Ventaja añadida: los ficheros traen las etiquetas temáticas que GDELT ya
asigna (IMMIGRATION, ENV_CLIMATECHANGE...). Más fiable que buscar palabras
en cada idioma, y reduce el sesgo idiomático.

Limitación: el fichero no incluye el titular. Se reconstruye desde la URL.
Funciona bien en medios de noticias; los artículos sin URL legible se
descartan y se cuentan aparte para poder reportarlo.

## Instalación

Coloca los dos archivos nuevos:
  - gkg_files.py   -> dentro de la carpeta `ingest`
  - test_gkg.py    -> dentro de la carpeta `ingest`
  - pipeline_gkg.py -> suelto en `polarmap` (junto a pipeline.py)

## Uso

Paso 1 — comprobar el parser (sin red, sin coste, 2 segundos):

    cd ~/Documents/polarmap
    python3 ingest/test_gkg.py

Debe terminar en "RESULTADO: TODO PASA".

Paso 2 — DRY RUN: descarga y filtra, pero NO gasta API de Claude.
Sirve para ver cuántos artículos consigues antes de pagar nada.

    python3 pipeline_gkg.py --countries ES --topics inmigracion --hours 24 --every 4 --dry-run

Verás el progreso de descarga y, al final, cuántos artículos útiles hay
y cinco ejemplos de titulares reconstruidos. LEE ESOS EJEMPLOS: si no
parecen titulares reales, algo va mal y hay que ajustar antes de seguir.

La primera vez tarda: son 24 ficheros de ~20-40 MB. Cuenta 10-20 minutos
según tu conexión. Quedan guardados en data/cache, así que la siguiente
ejecución sobre la misma ventana es instantánea.

Paso 3 — ejecución real (ya sí gasta API, céntimos):

    python3 pipeline_gkg.py --countries ES --topics inmigracion --hours 24 --every 4

## Si salen pocos artículos

Sube la ventana o la densidad, en este orden:

    --hours 48 --every 4     (48 ficheros, dos días)
    --hours 48 --every 2     (96 ficheros, más denso)
    --hours 24 --every 1     (96 ficheros, máxima densidad de un día)

## Espacio en disco

Cada fichero ocupa 20-40 MB. 48 ficheros ~ 1-2 GB en data/cache.
Para vaciar la caché cuando quieras:

    rm -rf data/cache

## Varios países a la vez

Aquí está la gran ventaja frente a la API: el fichero es GLOBAL, así que
descargar una vez sirve para todos los países. Añadir países no cuesta
descargas extra:

    python3 pipeline_gkg.py --countries ES,FR,DE,IT,US --topics inmigracion --hours 48 --every 4 --dry-run
