"""
Ingesta desde los ficheros GKG de GDELT (descarga directa, sin API).

POR QUÉ ESTE MÓDULO EXISTE:
La API DOC 2.0 de GDELT aplica un límite muy agresivo por IP y bloquea de
forma persistente. Los ficheros publicados son otra cosa: descargas normales
de HTTP, sin autenticación, sin límite de peticiones, sin bloqueos.

CÓMO FUNCIONA GDELT GKG:
Cada 15 minutos publican un fichero .zip con todos los artículos indexados
en el mundo en ese intervalo:
    http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip
Los timestamps van siempre en minutos 00, 15, 30, 45 y en UTC.

VENTAJA INESPERADA:
El GKG ya trae ETIQUETAS TEMÁTICAS asignadas por GDELT (V2Themes), como
IMMIGRATION o ENV_CLIMATECHANGE. Es mucho más robusto que buscar palabras
sueltas en varios idiomas: la etiqueta es la misma en español, alemán o
polaco, así que desaparece buena parte del sesgo idiomático.

LIMITACIÓN HONESTA:
El GKG no incluye el titular del artículo. Se reconstruye a partir del slug
de la URL (la parte legible del final), que en medios de noticias suele ser
el titular. No siempre funciona; los artículos sin slug legible se descartan
y eso se contabiliza para poder reportarlo.
"""

from __future__ import annotations

import io
import csv
import gzip
import time
import zipfile
import logging
import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

GKG_BASE = "http://data.gdeltproject.org/gdeltv2"
CACHE_DIR = Path("data/cache")

# Índices de las columnas que nos interesan dentro del GKG 2.1 (27 columnas,
# separadas por tabuladores, sin cabecera).
COL_DATE = 1           # V2.1DATE  (YYYYMMDDHHMMSS)
COL_SOURCE_NAME = 3    # V2SourceCommonName (dominio)
COL_DOC_ID = 4         # V2DocumentIdentifier (URL)
COL_THEMES = 7         # V1Themes: cada tema una vez, sin posición
COL_ENHANCED_THEMES = 8  # V2EnhancedThemes: "TEMA,offset;..." una entrada
                         # POR MENCIÓN. Es la que dice si el tema es central.
COL_TONE = 15          # V2Tone (lista separada por comas; el primero es el tono)

# Mapeo tema propio -> etiquetas GKG de GDELT.
# Se usa OR: basta con que aparezca una.
THEME_MAP = {
    "inmigracion": [
        "IMMIGRATION",
        "MIGRATION",
        "REFUGEES",
        "MOVEMENT_IMMIGRATION",
        "BORDER",
    ],
    "clima": [
        "ENV_CLIMATECHANGE",
        "CLIMATE_CHANGE",
        "ENV_CARBONCAPTURE",
        "MOVEMENT_ENVIRONMENTAL",
    ],
    "genero": [
        "SOC_GENDEREQUALITY",
        "WOMEN",
        "MOVEMENT_WOMENS_RIGHTS",
        "DISCRIMINATION_GENDER",
    ],
}

# Dominio -> país. Los TLD nacionales resuelven la mayoría de casos; los
# medios con dominio .com necesitan lista explícita.
TLD_COUNTRY = {
    ".es": "ES", ".fr": "FR", ".de": "DE", ".it": "IT", ".nl": "NL",
    ".pl": "PL", ".br": "BR", ".ar": "AR", ".mx": "MX", ".cl": "CL",
    ".uk": "GB", ".in": "IN",
}

DOMAIN_COUNTRY = {
    # España
    "elpais.com": "ES", "elmundo.es": "ES", "abc.es": "ES",
    "lavanguardia.com": "ES", "eldiario.es": "ES", "20minutos.es": "ES",
    "elconfidencial.com": "ES", "publico.es": "ES", "larazon.es": "ES",
    "libertaddigital.com": "ES", "okdiario.com": "ES", "infolibre.es": "ES",
    "elespanol.com": "ES", "vozpopuli.com": "ES", "elperiodico.com": "ES",
    "rtve.es": "ES", "cadenaser.com": "ES", "ondacero.es": "ES",
    "eldebate.com": "ES", "huffingtonpost.es": "ES", "niusdiario.es": "ES",
    # Estados Unidos
    "nytimes.com": "US", "washingtonpost.com": "US", "cnn.com": "US",
    "foxnews.com": "US", "nbcnews.com": "US", "usatoday.com": "US",
    "breitbart.com": "US", "huffpost.com": "US", "politico.com": "US",
    "npr.org": "US", "wsj.com": "US", "newsweek.com": "US",
    "thehill.com": "US", "msnbc.com": "US", "nypost.com": "US",
    # Reino Unido
    "bbc.com": "GB", "bbc.co.uk": "GB", "theguardian.com": "GB",
    "telegraph.co.uk": "GB", "dailymail.co.uk": "GB", "independent.co.uk": "GB",
    "thetimes.co.uk": "GB", "mirror.co.uk": "GB", "express.co.uk": "GB",
    # Francia
    "lemonde.fr": "FR", "lefigaro.fr": "FR", "liberation.fr": "FR",
    "france24.com": "FR", "lexpress.fr": "FR", "leparisien.fr": "FR",
    # Alemania
    "spiegel.de": "DE", "zeit.de": "DE", "faz.net": "DE", "welt.de": "DE",
    "sueddeutsche.de": "DE", "bild.de": "DE", "taz.de": "DE",
    # Italia
    "repubblica.it": "IT", "corriere.it": "IT", "lastampa.it": "IT",
    "ilfattoquotidiano.it": "IT", "ilgiornale.it": "IT", "ansa.it": "IT",
}


@dataclass
class Article:
    url: str
    title: str
    domain: str
    country: str
    language: str
    seendate: str
    topic: str
    tone: float | None = None

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Descarga de ficheros
# ---------------------------------------------------------------------------

def gkg_timestamps(hours_back: int, every_n: int = 1) -> list[str]:
    """
    Genera los timestamps de los ficheros GKG de las últimas `hours_back` horas.

    GDELT publica en :00, :15, :30 y :45 UTC. `every_n` permite muestrear en
    vez de descargarlo todo: every_n=4 baja un fichero por hora en lugar de
    cuatro, con una cuarta parte del tráfico.

    Muestrear NO sesga el índice mientras el muestreo sea sistemático y no
    dependa del contenido: se pierde volumen, no representatividad.
    """
    now = datetime.now(timezone.utc)
    # Redondear hacia abajo al múltiplo de 15 min anterior, y restar 15 min
    # más porque el fichero más reciente puede no estar publicado todavía.
    minute = (now.minute // 15) * 15
    anchor = now.replace(minute=minute, second=0, microsecond=0) - timedelta(minutes=15)

    stamps = []
    n_slots = int(hours_back * 4)
    for i in range(0, n_slots, every_n):
        t = anchor - timedelta(minutes=15 * i)
        stamps.append(t.strftime("%Y%m%d%H%M%S"))
    return stamps


def download_gkg(stamp: str, cache_dir: Path = CACHE_DIR,
                 timeout: int = 120, translation: bool = False) -> bytes | None:
    """
    Descarga (o lee de caché) un fichero GKG. El caché evita volver a bajar
    lo mismo entre ejecuciones, que es lo que hace viable iterar.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = "translation.gkg.csv.zip" if translation else "gkg.csv.zip"
    cached = cache_dir / f"{stamp}.{suffix}"

    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()

    url = f"{GKG_BASE}/{stamp}.{suffix}"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 404:
            # Normal: hay huecos en la serie de GDELT
            log.debug("no existe %s", stamp)
            return None
        r.raise_for_status()
        cached.write_bytes(r.content)
        return r.content
    except requests.RequestException as e:
        log.warning("fallo bajando %s: %s", stamp, e)
        return None


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------

def domain_to_country(domain: str) -> str | None:
    """Resuelve el país del medio: primero lista explícita, luego TLD."""
    if not domain:
        return None
    d = domain.lower().strip()
    if d.startswith("www."):
        d = d[4:]
    if d in DOMAIN_COUNTRY:
        return DOMAIN_COUNTRY[d]
    for tld, country in TLD_COUNTRY.items():
        if d.endswith(tld):
            return country
    return None


# Restos que los CMS meten en el slug y no son parte del titular.
_SLUG_PREFIXES = {"noticia", "noticias"}           # Europa Press y otros
_SLUG_SUFFIXES = {"nt", "ntrc", "nts", "ntv", "ntb", "ntx", "ctx"}  # Vocento
_SLUG_CODE = re.compile(r"^(\d[a-z]{1,4}|[a-z]\d[a-z0-9]{0,2})$")  # 1tna, 1b, p7m
_SLUG_HASH = re.compile(r"^(?=.*\d)[0-9a-f]{12,}$")  # 202609226ab2...


def _is_slug_junk(word: str) -> bool:
    return bool(word.isdigit() or _SLUG_HASH.match(word)
                or _SLUG_CODE.match(word))


def slug_to_title(url: str) -> str:
    """
    Reconstruye un titular aproximado desde el slug de la URL.

    'https://elpais.com/espana/2026-08-05/el-gobierno-aprueba-la-reforma.html'
        -> 'el gobierno aprueba la reforma'

    Devuelve cadena vacía si no hay slug utilizable (URLs con solo un id
    numérico, por ejemplo). Esos artículos se descartan y se cuentan aparte:
    inventarse un titular sería mucho peor que perder el dato.
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    if not path:
        return ""

    seg = path.rstrip("/").split("/")[-1]
    # quitar extensión
    for ext in (".html", ".htm", ".php", ".amp", ".shtml"):
        if seg.endswith(ext):
            seg = seg[: -len(ext)]
    seg = seg.replace("_", "-")
    parts = [p for p in seg.split("-") if p]

    # descartar fragmentos que son solo dígitos, hashes o ids cortos
    words = [p.lower() for p in parts if len(p) > 1 and not _is_slug_junk(p.lower())]
    while words and words[0] in _SLUG_PREFIXES:
        words.pop(0)
    while words and words[-1] in _SLUG_SUFFIXES:
        words.pop()
    if len(words) < 4:
        return ""
    # slugs demasiado largos suelen arrastrar ids: cortar
    words = words[:20]
    return " ".join(words)


# Secciones que nunca son debate público. Se miran los segmentos de la
# ruta (no el slug): /deportes/futbol/... o /gente/... se descartan.
SOFT_SECTIONS = {
    "deportes", "deporte", "futbol", "baloncesto", "motor", "gente",
    "famosos", "corazon", "horoscopo", "loterias", "loteria", "el-tiempo",
    "tiempo", "television", "tv", "series", "programas", "cine", "musica",
    "gastronomia", "recetas", "cocina", "viajes", "estilo", "moda",
    "belleza", "lifestyle", "ocio", "planes", "escaparate", "compras",
    "ofertas", "mascotas",
}


def is_soft_section(url: str) -> bool:
    """¿El artículo está en una sección blanda (deportes, famosos...)?"""
    try:
        segs = [x for x in urlparse(url).path.lower().split("/") if x]
    except ValueError:
        return False
    return any(seg in SOFT_SECTIONS for seg in segs[:-1])


def title_key(title: str) -> str:
    """Clave para detectar la misma pieza sindicada en varios medios."""
    return " ".join(title.split())


def matches_topic(themes_field: str, topic: str) -> bool:
    """¿Este artículo lleva alguna etiqueta temática del tema buscado?"""
    if not themes_field:
        return False
    up = themes_field.upper()
    return any(tag in up for tag in THEME_MAP.get(topic, []))


def parse_gkg_zip(raw: bytes, topic: str,
                  countries: set[str]) -> tuple[list[Article], dict]:
    """
    Extrae los artículos relevantes de un fichero GKG.

    Devuelve (artículos, estadísticas). Las estadísticas permiten saber cuánto
    se está perdiendo por el camino, que es información necesaria para
    interpretar el índice.
    """
    stats = {"rows": 0, "topic_match": 0, "country_match": 0,
             "no_title": 0, "kept": 0}
    out: list[Article] = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = zf.namelist()[0]
        data = zf.read(name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError) as e:
        log.warning("zip ilegible: %s", e)
        return out, stats

    reader = csv.reader(io.StringIO(data), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        stats["rows"] += 1
        if len(row) <= COL_TONE:
            continue

        if not matches_topic(row[COL_THEMES], topic):
            continue
        stats["topic_match"] += 1

        domain = row[COL_SOURCE_NAME]
        country = domain_to_country(domain)
        if country is None or country not in countries:
            continue
        stats["country_match"] += 1

        url = row[COL_DOC_ID]
        title = slug_to_title(url)
        if not title:
            stats["no_title"] += 1
            continue

        tone = None
        try:
            tone = float(row[COL_TONE].split(",")[0])
        except (ValueError, IndexError):
            pass

        out.append(Article(
            url=url, title=title, domain=domain, country=country,
            language="", seendate=row[COL_DATE], topic=topic, tone=tone,
        ))
        stats["kept"] += 1

    return out, stats


# ---------------------------------------------------------------------------
# Interfaz principal
# ---------------------------------------------------------------------------

class GKGClient:
    """Sustituto de GDELTClient que usa ficheros en vez de la API."""

    def __init__(self, cache_dir: Path = CACHE_DIR, pause: float = 0.5):
        self.cache_dir = Path(cache_dir)
        self.pause = pause  # cortesía con el servidor, no es un límite impuesto

    def fetch(self, countries: list[str], topic: str, hours_back: int = 24,
              every_n: int = 4, max_files: int = 60) -> tuple[list[Article], dict]:
        """
        Descarga y filtra artículos de varios países a la vez.

        A diferencia de la API, aquí una sola pasada sirve para TODOS los
        países: el fichero es global. Eso lo hace mucho más eficiente cuando
        el mapa crece.

        every_n=4  -> un fichero por hora (recomendado para empezar)
        max_files  -> tope de seguridad para no llenar el disco
        """
        stamps = gkg_timestamps(hours_back, every_n)[:max_files]
        cset = set(countries)

        all_articles: dict[str, Article] = {}
        totals = {"files_ok": 0, "files_missing": 0, "rows": 0,
                  "topic_match": 0, "country_match": 0, "no_title": 0, "kept": 0}

        for i, stamp in enumerate(stamps, 1):
            # GDELT publica DOS series por cada intervalo:
            #   - la principal, con medios en ingles
            #   - la 'translation', con medios en el resto de idiomas
            # La prensa espanola, francesa, alemana, etc. esta en la segunda.
            # Bajar solo la primera fue el motivo de que salieran 12 articulos
            # espanoles en vez de cientos.
            got_any = False
            for is_trans in (False, True):
                raw = download_gkg(stamp, self.cache_dir, translation=is_trans)
                if raw is None:
                    continue
                got_any = True
                arts, stats = parse_gkg_zip(raw, topic, cset)
                for k in ("rows", "topic_match", "country_match", "no_title", "kept"):
                    totals[k] += stats[k]
                for a in arts:
                    all_articles[a.url] = a  # dedup por URL
            if got_any:
                totals["files_ok"] += 1
            else:
                totals["files_missing"] += 1

            if i % 5 == 0 or i == len(stamps):
                log.info("  %d/%d ficheros | %d articulos utiles",
                         i, len(stamps), len(all_articles))
            time.sleep(self.pause)

        return list(all_articles.values()), totals


def source_concentration(articles: list[Article]) -> dict:
    """Igual que en el módulo de API: detecta si un par de medios dominan."""
    from collections import Counter
    if not articles:
        return {"n": 0, "n_domains": 0, "hhi": float("nan"),
                "top_share": float("nan"), "reliable": False}
    counts = Counter(a.domain for a in articles)
    total = sum(counts.values())
    shares = [c / total for c in counts.values()]
    hhi = sum(s ** 2 for s in shares)
    return {
        "n": total,
        "n_domains": len(counts),
        "hhi": float(hhi),
        "top_share": float(max(shares)),
        "top_domains": counts.most_common(5),
        "reliable": bool(len(counts) >= 8 and hhi < 0.25),
    }
