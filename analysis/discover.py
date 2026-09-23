"""
Descubrimiento automático de temas polarizantes.

Este módulo es el corazón del cambio de enfoque: en vez de decidir
de antemano qué temas analizar, lee TODO lo que publican los medios
de un país y descubre sobre qué se debate.

Funciona en dos pasadas sobre los mismos ficheros GKG cacheados:

  Pasada 1 — CENSO (barata, sin API):
    Lee todas las filas, solo cuenta etiquetas temáticas por país.
    Resultado: "en España esta semana hay 420 artículos sobre
    IMMIGRATION, 310 sobre TAX_POLICY, 195 sobre HOUSING..."
    Fusiona etiquetas que van juntas (IMMIGRATION + REFUGEES = un tema).

  Pasada 2 — ANÁLISIS (usa API de Claude, selectiva):
    Solo para los N temas más frecuentes: extrae titulares, Claude
    define el eje de debate, clasifica posturas, calcula métricas.

NO modifica ninguno de los módulos existentes (gkg_files, metrics,
stance). Todo lo que necesita lo importa.
"""

from __future__ import annotations

import io
import csv
import json
import os
import re
import time
import logging
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from itertools import combinations
from pathlib import Path

import numpy as np
import requests

from ingest.gkg_files import (
    download_gkg, gkg_timestamps, domain_to_country, slug_to_title,
    source_concentration, Article, CACHE_DIR,
    COL_DATE, COL_SOURCE_NAME, COL_DOC_ID, COL_THEMES, COL_TONE,
)

log = logging.getLogger(__name__)


# ===================================================================
# ETIQUETAS DE RUIDO
# ===================================================================
# Estas etiquetas aparecen en casi todo y no dicen de qué va el debate.
# La lista NO decide qué temas importan. Solo quita las que son tan
# genéricas que contaminarían la agrupación.
#
# Ejemplo: LEADER aparece cada vez que se menciona a un presidente,
# pero no te dice si el debate es sobre impuestos o sobre inmigración.
# ===================================================================

NOISE_THEMES = {
    # ── Conceptos demasiado amplios ──
    "GENERAL_GOVERNMENT", "GOVERNMENT", "GOVERNANCE",
    "POLITICS", "POLITICAL_PARTY", "POLITICAL_TURMOIL",
    "LEGISLATION", "REGULATION", "POLICY",
    "DEMOCRACY", "ELECTION", "VOTE", "CAMPAIGN",
    # ── Personas genéricas ──
    "LEADER", "HEADS_OF_STATE", "ELECTED_OFFICIAL",
    "CELEBRITY", "PERSON",
    # ── Medios (meta, no tema) ──
    "MEDIA", "MEDIA_MSM", "MEDIA_SOCIAL", "INTERNET",
    # ── Relaciones genéricas ──
    "INTERNATIONAL_RELATIONS", "DIPLOMACY", "FOREIGN_POLICY",
    "RELATIONS", "COOPERATION",
    # ── Categorías enormes sin eje de debate claro ──
    "ECONOMY", "HEALTH", "SECURITY", "EDUCATION",
    "CRIME", "JUSTICE", "LAW", "RIGHTS",
    # ── Eventos / sucesos, no debates ──
    "NATURAL_DISASTER", "ACCIDENT", "DISASTER",
    "KILL", "ARREST", "KIDNAP", "ASSAULT",
    "WOUND", "DIE", "SEIZE", "THREATEN",
    # ── Sentimiento / tono (descriptores, no temas) ──
    "AFFECT", "FEAR", "HAPPINESS", "ANGER", "SADNESS",
    # ── Descriptores sociales genéricos ──
    "TOURISM", "TOURISM_ARRIVALS", "TOURISM_RECEIPTS",
    "AGRICULTURE", "FARMING",
    "SPORT", "SPORTS",
    "WEATHER", "FORECAST",
    # ── Etiquetas internas de GDELT (no son temas reales) ──
    "SELF_IDENTIFIED_ENVIRON_DISASTER",
    "SELF_IDENTIFIED_TERROR_ATTACK",
}

# Prefijos: si una etiqueta EMPIEZA con alguno de estos, se descarta.
# Esto captura familias enteras de una vez:
#   TAX_FNCACT_PRESIDENT, TAX_WORLDINSECTS_BEE, EPU_POLICY_MONETARY...
NOISE_PREFIXES = (
    "TAX_FNCACT",        # taxonomía interna de GDELT (personas/cargos)
    "TAX_ETHNICITY",     # taxonomía de etnias (clasificación, no debate)
    "TAX_RELIGION",      # taxonomía religiosa
    "TAX_WORLDLANGUAGE", # idiomas del mundo
    "TAX_WORLDMAMMALS",  # mamíferos (zoología, no debate)
    "TAX_WORLDINSECTS",  # insectos
    "TAX_WORLDFISH",     # peces
    "TAX_WORLDBIRDS",    # aves
    "TAX_WORLDREPTILES", # reptiles
    "TAX_WORLDAMPHIBIANS",
    "TAX_DISEASE",       # enfermedades (clasificación médica)
    "EPU_",              # índice de incertidumbre política
    "WB_",               # clasificación del Banco Mundial
    "UNGP_",             # principios de la ONU
    "USPEC_",            # clasificaciones internas
    "CRISISLEX_",        # vocabulario de crisis (lingüístico)
    "MANMADE_DISASTER",
    "GENERAL_",          # GENERAL_HEALTH, GENERAL_GOVERNMENT, etc.
    "GEN_",              # GEN_HOLIDAY, GEN_WEEKEND, etc.
    "SOC_",              # SOC_EXPRESSREGRET, SOC_CONDOLENCES, etc.
    "ARMEDCONFLICT",     # demasiado amplio (cubre todo tipo de violencia)
    "CONFLICT",          # igual
    "CORRUPTION",        # a veces debate, pero GDELT lo pone en todo
    "MOVEMENT_",         # MOVEMENT_SOCIAL, etc. — demasiado genérico
)


def is_noise_theme(theme: str) -> bool:
    """¿Es esta etiqueta demasiado genérica para ser útil como tema?"""
    if theme in NOISE_THEMES:
        return True
    return any(theme.startswith(p) for p in NOISE_PREFIXES)


# ===================================================================
# EXTRACCIÓN DE ETIQUETAS DE UN CAMPO GKG
# ===================================================================

def extract_themes(themes_field: str) -> list[str]:
    """
    Extrae nombres de etiquetas del campo V2Themes del GKG.

    El campo viene en dos formatos posibles:
      THEME1,offset;THEME2,offset;...   (V2EnhancedThemes)
      THEME1;THEME2;THEME3              (V2Themes plano)

    Devuelve solo los nombres, en mayúsculas, sin duplicados,
    sin etiquetas de ruido.
    """
    if not themes_field:
        return []
    seen = set()
    result = []
    for part in themes_field.split(";"):
        # Tomar solo la parte antes de la coma (ignorar offset si existe)
        name = part.split(",")[0].strip().upper()
        if name and name not in seen and not is_noise_theme(name):
            seen.add(name)
            result.append(name)
    return result


# ===================================================================
# PASADA 1: CENSO DE TEMAS POR PAÍS
# ===================================================================
# Lee los ficheros GKG y cuenta cuántas veces aparece cada etiqueta
# temática en cada país. No guarda titulares ni URLs — solo contadores.
# Memoria mínima: un diccionario de contadores.
# ===================================================================

@dataclass
class ThemeCensus:
    """Resultado del censo: cuántas veces aparece cada tema por país."""
    counts: dict         # {country: {theme: n_artículos}}
    cooccur: dict        # {country: {(thA, thB): n_veces_juntas}}
    total_rows: int      # filas leídas en total (todos los países)
    total_country: int   # filas que pertenecen a algún país pedido


def _census_one_zip(raw: bytes, countries: set[str]
                    ) -> tuple[dict, dict, int, int]:
    """
    Lee UN fichero GKG y cuenta temas por país. NO guarda titulares.

    Devuelve (counts, cooccur, n_rows, n_country_match).
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    cooccur: dict[str, Counter] = defaultdict(Counter)
    n_rows = 0
    n_country = 0

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = zf.namelist()[0]
        data = zf.read(name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError) as e:
        log.warning("zip ilegible en censo: %s", e)
        return counts, cooccur, 0, 0

    # Subir el límite de tamaño de campo para filas con URLs enormes
    csv.field_size_limit(10 * 1024 * 1024)

    reader = csv.reader(
        io.StringIO(data), delimiter="\t", quoting=csv.QUOTE_NONE
    )
    for row in reader:
        n_rows += 1
        if len(row) <= COL_TONE:
            continue

        domain = row[COL_SOURCE_NAME]
        country = domain_to_country(domain)
        if country is None or country not in countries:
            continue
        n_country += 1

        themes = extract_themes(row[COL_THEMES])
        if not themes:
            continue

        # Contar cada tema
        for th in themes:
            counts[country][th] += 1

        # Co-ocurrencia: pares ORDENADOS de etiquetas en el mismo artículo.
        # Solo guardamos un par por combinación (A,B) con A < B
        # para no duplicar.
        if len(themes) >= 2:
            unique = sorted(set(themes))
            for a, b in combinations(unique, 2):
                cooccur[country][(a, b)] += 1

    return dict(counts), dict(cooccur), n_rows, n_country


def run_census(countries: list[str], hours_back: int = 24,
               every_n: int = 4, max_files: int = 60,
               cache_dir: Path = CACHE_DIR,
               pause: float = 0.5) -> ThemeCensus:
    """
    Pasada 1 completa: recorre los ficheros GKG y cuenta temas por país.

    Usa los mismos ficheros cacheados que la ingesta normal. Si ya se
    descargaron antes, esto tarda segundos (solo lectura local).

    Parámetros:
      countries  : ["ES", "FR", "DE", ...]
      hours_back : ventana temporal en horas
      every_n    : muestreo (4 = un fichero por hora)
      max_files  : tope de seguridad
      pause      : pausa entre descargas (cortesía, no límite)
    """
    stamps = gkg_timestamps(hours_back, every_n)[:max_files]
    cset = set(countries)

    all_counts: dict[str, Counter] = defaultdict(Counter)
    all_cooccur: dict[str, Counter] = defaultdict(Counter)
    total_rows = 0
    total_country = 0

    for i, stamp in enumerate(stamps, 1):
        # Recordatorio: GDELT publica DOS series por intervalo:
        #   - Principal (medios en inglés)
        #   - Translation (resto de idiomas: español, francés, alemán...)
        # HAY QUE BAJAR LAS DOS o se pierden cientos de artículos.
        for is_trans in (False, True):
            raw = download_gkg(stamp, cache_dir, translation=is_trans)
            if raw is None:
                continue
            counts, cooccur, nr, nc = _census_one_zip(raw, cset)
            total_rows += nr
            total_country += nc
            for country, ctr in counts.items():
                all_counts[country] += ctr
            for country, ctr in cooccur.items():
                all_cooccur[country] += ctr

        if i % 5 == 0 or i == len(stamps):
            log.info("  censo: %d/%d ficheros | %d filas | %d de países pedidos",
                     i, len(stamps), total_rows, total_country)
        time.sleep(pause)

    return ThemeCensus(
        counts=dict(all_counts),
        cooccur=dict(all_cooccur),
        total_rows=total_rows,
        total_country=total_country,
    )


# ===================================================================
# AGRUPACIÓN DE ETIQUETAS POR CO-OCURRENCIA
# ===================================================================
# IMMIGRATION y REFUGEES aparecen juntas en el 80% de los artículos.
# No son dos temas: son dos etiquetas del mismo debate. Las fusionamos
# usando un algoritmo simple (Union-Find) que agrupa transitivamente:
# si A va con B y B va con C, los tres son el mismo grupo.
# ===================================================================

@dataclass
class TopicGroup:
    """Un grupo de etiquetas que representan el mismo tema de debate."""
    tags: list           # etiquetas GDELT fusionadas
    count: int = 0       # nº de artículos (aprox, del censo)
    label: str = ""      # nombre del debate (lo pone Claude después)
    pole_a: str = ""     # un lado del debate (lo pone Claude)
    pole_b: str = ""     # el otro lado (lo pone Claude)


class _UnionFind:
    """Estructura de datos para agrupar elementos transitivamente."""
    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def build_topic_groups(counts: Counter, cooccur: Counter,
                       min_articles: int = 20,
                       merge_threshold: float = 0.50,
                       max_group_size: int = 5,
                       ) -> list[TopicGroup]:
    """
    Agrupa etiquetas por co-ocurrencia y devuelve los grupos ordenados.

    CUÁNDO SE FUSIONAN DOS ETIQUETAS:
    Solo si aparecen juntas más del 50% de las veces EN AMBAS
    DIRECCIONES. Ejemplo:
      IMMIGRATION aparece 300 veces
      REFUGEES aparece 120 veces
      Juntas aparecen 100 veces
      → 100/300 = 33% (dirección A→B)
      → 100/120 = 83% (dirección B→A)
      → Mínimo de las dos = 33% → NO se fusionan (< 50%)

    Esto evita el problema de las cadenas: antes, con el mínimo al
    revés, A se fusionaba con B, B con C, y acababa todo en un solo
    grupo monstruoso de 49 etiquetas.

    LÍMITE DE TAMAÑO:
    Un grupo no puede tener más de 5 etiquetas. Si crece más, es
    señal de que la fusión está juntando cosas distintas.

    min_articles filtra etiquetas con pocas menciones.
    """
    # Solo etiquetas con suficientes artículos
    viable = {th for th, n in counts.items() if n >= min_articles}
    if not viable:
        return []

    # Calcular afinidad BIDIRECCIONAL entre pares de etiquetas.
    # Solo fusionar si AMBAS direcciones superan el umbral.
    merge_pairs = []
    for (a, b), n_both in cooccur.items():
        if a not in viable or b not in viable:
            continue
        rate_a = n_both / counts[a] if counts[a] > 0 else 0
        rate_b = n_both / counts[b] if counts[b] > 0 else 0
        # Exigir que el MÍNIMO de las dos direcciones supere el umbral.
        # Así, una etiqueta grande no absorbe a una pequeña a menos que
        # realmente estén hablando de lo mismo.
        if min(rate_a, rate_b) >= merge_threshold:
            merge_pairs.append((a, b, min(rate_a, rate_b)))

    # Ordenar por afinidad descendente: fusionar primero los pares
    # más claramente relacionados.
    merge_pairs.sort(key=lambda x: x[2], reverse=True)

    # Fusionar con límite de tamaño
    uf = _UnionFind()
    group_sizes: dict[str, int] = {}  # root -> size

    for a, b, _ in merge_pairs:
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue  # ya están en el mismo grupo
        size_a = group_sizes.get(ra, 1)
        size_b = group_sizes.get(rb, 1)
        if size_a + size_b > max_group_size:
            continue  # fusionarlos crearía un grupo demasiado grande
        uf.union(a, b)
        new_root = uf.find(a)
        group_sizes[new_root] = size_a + size_b

    # Construir los grupos
    groups_by_root: dict[str, set[str]] = defaultdict(set)
    for th in viable:
        root = uf.find(th)
        groups_by_root[root].add(th)

    # Contar artículos por grupo.
    # Usamos el MÁXIMO del grupo como proxy (no la suma, porque un
    # artículo con 3 etiquetas del grupo se contaría 3 veces).
    result = []
    for tags in groups_by_root.values():
        group_count = max(counts[th] for th in tags)
        result.append(TopicGroup(
            tags=sorted(tags),
            count=group_count,
        ))

    # Ordenar de mayor a menor volumen
    result.sort(key=lambda g: g.count, reverse=True)
    return result


# ===================================================================
# PASADA 2: EXTRAER ARTÍCULOS DE UN GRUPO TEMÁTICO
# ===================================================================
# Vuelve a los mismos ficheros GKG (ya cacheados en disco, así que
# es instantáneo) y extrae los artículos cuyas etiquetas coincidan
# con alguna del grupo.
# ===================================================================

def fetch_articles_for_group(tags: list[str], countries: list[str],
                             hours_back: int = 24, every_n: int = 4,
                             max_files: int = 60,
                             cache_dir: Path = CACHE_DIR
                             ) -> dict[str, list[Article]]:
    """
    Pasada 2: extrae artículos de un grupo temático, por país.

    Como los ficheros ya están en disco (los bajó el censo), esto es
    casi instantáneo. No descarga nada nuevo.

    Devuelve: {country: [Article, Article, ...]}
    """
    stamps = gkg_timestamps(hours_back, every_n)[:max_files]
    cset = set(countries)
    tags_upper = {t.upper() for t in tags}

    # Dedup por URL dentro de cada país
    seen_urls: dict[str, set[str]] = defaultdict(set)
    by_country: dict[str, list[Article]] = defaultdict(list)

    csv.field_size_limit(10 * 1024 * 1024)

    for stamp in stamps:
        for is_trans in (False, True):
            raw = download_gkg(stamp, cache_dir, translation=is_trans)
            if raw is None:
                continue

            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
                name = zf.namelist()[0]
                data = zf.read(name).decode("utf-8", errors="replace")
            except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
                continue

            reader = csv.reader(
                io.StringIO(data), delimiter="\t",
                quoting=csv.QUOTE_NONE,
            )
            for row in reader:
                if len(row) <= COL_TONE:
                    continue

                domain = row[COL_SOURCE_NAME]
                country = domain_to_country(domain)
                if country is None or country not in cset:
                    continue

                # ¿Tiene alguna etiqueta del grupo?
                row_themes = extract_themes(row[COL_THEMES])
                if not any(t in tags_upper for t in row_themes):
                    continue

                url = row[COL_DOC_ID]
                if url in seen_urls[country]:
                    continue
                seen_urls[country].add(url)

                title = slug_to_title(url)
                if not title:
                    continue

                tone = None
                try:
                    tone = float(row[COL_TONE].split(",")[0])
                except (ValueError, IndexError):
                    pass

                by_country[country].append(Article(
                    url=url, title=title, domain=domain,
                    country=country, language="",
                    seendate=row[COL_DATE],
                    topic="|".join(sorted(tags)),
                    tone=tone,
                ))

    return dict(by_country)


# ===================================================================
# DESCUBRIMIENTO DEL EJE DE DEBATE
# ===================================================================
# Una sola llamada a Claude. Le mandamos ~40 titulares de un grupo
# temático y le pedimos que identifique el eje de debate: nombre
# del tema y los dos polos (las dos posturas enfrentadas).
#
# Esto reemplaza el diccionario AXES que antes se escribía a mano.
# ===================================================================

API_URL = "https://api.anthropic.com/v1/messages"
DISCOVER_MODEL = "claude-sonnet-4-6"

# Reintentos ante errores transitorios (red, 429, 5xx).
API_RETRIES = 3
API_BACKOFF = 2.0  # segundos, se duplica en cada intento


class FatalAPIError(RuntimeError):
    """Error que no se arregla reintentando (key inválida o revocada,
    petición mal formada). Aborta la corrida: publicar un mapa gris
    por una key caducada es peor que no publicar."""


class TransientAPIError(RuntimeError):
    """La API no respondió bien tras agotar los reintentos."""


def _call_api(body: dict, api_key: str, timeout: int) -> str:
    """POST a Messages con reintentos. Devuelve el texto de la respuesta."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last = None
    for attempt in range(API_RETRIES):
        if attempt:
            time.sleep(API_BACKOFF * 2 ** (attempt - 1))
        try:
            resp = requests.post(API_URL, headers=headers, json=body,
                                 timeout=timeout)
        except requests.RequestException as e:
            last = f"red: {e}"
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            continue
        if resp.status_code >= 400:
            raise FatalAPIError(
                f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["content"][0]["text"].strip()
        except (ValueError, KeyError, IndexError, TypeError) as e:
            last = f"respuesta sin texto: {e}"
    raise TransientAPIError(
        f"sin respuesta válida tras {API_RETRIES} intentos ({last})")


def discover_axis(titles: list[str], tag_names: list[str],
                  country_name: str, api_key: str | None = None,
                  max_titles: int = 40) -> dict | None:
    """
    Pide a Claude que identifique el eje de debate de un grupo temático.

    Le mandamos una muestra de titulares y las etiquetas GDELT del grupo.
    Claude devuelve:
      label  : nombre del debate (2-5 palabras, en el idioma del país)
      pole_a : un lado del debate (una frase corta)
      pole_b : el otro lado

    Si no hay debate claro (son noticias puramente informativas sin
    posicionamiento), devuelve None. Esto es correcto: no todo tema
    genera polarización.

    Un fallo de la API NO es "sin debate": lanza FatalAPIError o
    TransientAPIError para que el pipeline lo distinga.

    Coste: una sola llamada por tema, ~300 tokens. Barato.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY. Expórtala con:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    # Muestra representativa: 40 titulares bastan para que Claude
    # identifique el debate. Mandar 400 no mejora nada y cuesta más.
    sample = titles[:max_titles]
    titles_block = "\n".join(f"- {t}" for t in sample)
    tags_str = ", ".join(tag_names)

    prompt = f"""Aquí tienes {len(sample)} titulares de medios de {country_name} de esta semana.
Todos están relacionados con estos temas: {tags_str}.

{titles_block}

Lee los titulares y responde EXACTAMENTE en este formato (tres líneas, nada más):

TOPIC_LABEL: [nombre corto del debate público, 2-5 palabras, en el idioma principal del país]
POLE_A: [una frase corta describiendo un lado del debate]
POLE_B: [una frase corta describiendo el lado opuesto]

Reglas:
- Los dos polos deben representar posturas REALES que la gente defiende en el debate público.
- NO uses "a favor / en contra" genérico. Sé específico.
- Los polos deben ser lo bastante claros para que un lector reconozca qué lado es cuál.
- El TOPIC_LABEL debe ser en el idioma del país (español para España, francés para Francia, etc.)
- Define el eje de forma AMPLIA, no limitada a un evento concreto. 
  Ejemplo: "Política migratoria" es mejor que "Crisis en Ceuta del martes".
  Un eje amplio permite clasificar más titulares.

Responde TOPIC_LABEL: none SOLO si los titulares son puramente informativos
SIN NINGUNA dimensión política, social o de opinión (por ejemplo, resultados
deportivos, el tiempo meteorológico, obituarios). Si hay CUALQUIER debate
subyacente, aunque sea sutil, identifícalo."""

    body = {
        "model": DISCOVER_MODEL,
        "max_tokens": 300,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }
    text = _call_api(body, key, timeout=30)

    # Parsear la respuesta (formato clave: valor en cada línea)
    parsed = {}
    for line in text.split("\n"):
        line = line.strip()
        if ":" in line:
            key_name, _, value = line.partition(":")
            parsed[key_name.strip().upper()] = value.strip()

    label = parsed.get("TOPIC_LABEL", "")
    if not label or label.lower() == "none":
        log.info("  sin debate claro para %s", tags_str)
        return None

    pole_a = parsed.get("POLE_A", "")
    pole_b = parsed.get("POLE_B", "")
    if not pole_a or not pole_b:
        log.warning("  respuesta incompleta para %s: %s", tags_str, text[:200])
        return None

    return {"label": label, "pole_a": pole_a, "pole_b": pole_b}


# ===================================================================
# CLASIFICACIÓN DE POSTURAS CON EJES DINÁMICOS
# ===================================================================
# En vez de modificar stance.py, esta función llama directamente a la
# API de Claude con el mismo diseño de prompt que StanceClassifier
# pero usando el eje que se acaba de descubrir.
#
# Así no toco ninguno de tus archivos existentes.
# ===================================================================

CLASSIFY_MODEL = "claude-sonnet-4-6"

# Máximo de titulares por lote. 20 es el punto dulce entre coste
# y velocidad (probado en la versión anterior).
BATCH_SIZE = 20


@dataclass
class StanceResult:
    """Resultado de clasificar un titular."""
    title: str
    stance: float        # -1 (polo A) a +1 (polo B)
    hostility: float     # 0 (neutro) a 1 (hostil)
    confidence: float    # 0 a 1
    off_topic: bool      # True si el titular no va del tema
    failed: bool = False  # True si NO se pudo clasificar (API o parseo).
                          # No es off-topic: es un dato perdido.


def classify_stances(titles: list[str], pole_a: str, pole_b: str,
                     language: str = "spa",
                     api_key: str | None = None
                     ) -> list[StanceResult]:
    """
    Clasifica la postura de cada titular en el eje pole_a ↔ pole_b.

    Funciona exactamente como StanceClassifier.classify_batch pero
    acepta polos dinámicos (generados por discover_axis) en vez de
    buscarlos en el diccionario AXES.

    El prompt pide tres cosas por titular:
      stance     : posición en el eje (-1.0 a +1.0)
      hostility  : nivel de hostilidad (0.0 a 1.0)
      confidence : confianza del clasificador (0.0 a 1.0)

    Y marca off_topic si el titular no tiene relación con el debate.

    Envía en lotes de 20 para abaratar. Una pausa entre lotes
    para no saturar la API.

    Un lote que falla tras los reintentos vuelve con failed=True (no
    off_topic). Una key inválida lanza FatalAPIError y corta todo.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY")

    all_results: list[StanceResult] = []

    for i in range(0, len(titles), BATCH_SIZE):
        batch = titles[i:i + BATCH_SIZE]
        batch_results = _classify_batch(batch, pole_a, pole_b,
                                        language, key)
        all_results.extend(batch_results)

        n_done = min(i + BATCH_SIZE, len(titles))
        log.info("    clasificados %d/%d titulares", n_done, len(titles))
        time.sleep(0.5)

    return all_results


def _classify_batch(titles: list[str], pole_a: str, pole_b: str,
                    language: str, api_key: str
                    ) -> list[StanceResult]:
    """Clasifica un lote de hasta 20 titulares en una sola llamada."""

    numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(titles))

    prompt = f"""Eres un analista de postura (stance) en el debate público.

EJE DE DEBATE:
  Polo A (-1): {pole_a}
  Polo B (+1): {pole_b}

Para cada titular, evalúa:
  stance: posición en el eje (-1.0 = polo A extremo, 0.0 = centro/neutral, +1.0 = polo B extremo)
  hostility: nivel de hostilidad o agresividad en el lenguaje (0.0 = neutro, 1.0 = muy hostil)
  confidence: tu confianza en la clasificación (0.0 = no sé, 1.0 = muy seguro)

REGLAS PARA off_topic:
- off_topic=true SOLO si el titular habla de un tema COMPLETAMENTE DISTINTO
  (ejemplo: un resultado deportivo cuando el eje es sobre migración).
- Si el titular habla del tema pero de forma informativa sin tomar postura,
  eso es stance=0.0 con off_topic=false. Un artículo neutral ES un dato válido.
- Si el titular menciona el tema de pasada o tangencialmente,
  pon off_topic=false con stance=0.0 y confidence baja (0.2-0.4).

TITULARES:
{numbered}

Responde SOLO con un JSON array. Cada elemento es un objeto con: id (número del titular), stance, hostility, confidence, off_topic.
Ejemplo: [{{"id":1,"stance":-0.6,"hostility":0.2,"confidence":0.8,"off_topic":false}}]

IMPORTANTE:
- Mide POSTURA, no sentimiento. Un terremoto es negativo pero no tiene postura.
- El centro (0.0) es una posición legítima, no un fallo. La mayoría de titulares informativos serán cercanos a 0.
- Hostilidad es agresividad del lenguaje, no desacuerdo.
- Los titulares pueden estar en cualquier idioma. Evalúa el contenido, no el idioma.
- Sé GENEROSO con off_topic=false. Marca off_topic=true solo si realmente no tiene NADA que ver."""

    body = {
        "model": CLASSIFY_MODEL,
        "max_tokens": 2000,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        text = _call_api(body, api_key, timeout=60)
    except TransientAPIError as e:
        log.error("lote de %d titulares SIN clasificar: %s", len(titles), e)
        return _failed(titles)

    # Parsear el JSON de la respuesta.
    # Claude a veces envuelve el JSON en ```json ... ```
    # o añade texto antes/después. Buscamos el array.
    return _parse_classify_response(text, titles)


def _failed(titles: list[str]) -> list[StanceResult]:
    return [StanceResult(t, 0.0, 0.0, 0.0, off_topic=True, failed=True)
            for t in titles]


def _parse_classify_response(text: str, titles: list[str]
                             ) -> list[StanceResult]:
    """
    Parsea la respuesta de clasificación. Robusto ante formatos
    inesperados (texto extra, markdown, JSON parcial).
    """
    # Limpiar envoltorio markdown si lo hay
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Buscar el array JSON (puede haber texto antes o después)
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        log.error("no se encontró JSON array en la respuesta")
        return _failed(titles)

    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log.error("JSON inválido en respuesta de clasificación: %s", e)
        return _failed(titles)
    if not isinstance(items, list):
        log.error("la respuesta de clasificación no es un array")
        return _failed(titles)

    # Mapear resultados por id
    by_id = {}
    for item in items:
        if isinstance(item, dict) and "id" in item:
            by_id[item["id"]] = item

    results = []
    for j, title in enumerate(titles):
        item = by_id.get(j + 1)
        if item is None:
            # Respuesta cortada o id que falta: dato perdido, no off-topic.
            results.append(StanceResult(title, 0.0, 0.0, 0.0,
                                        off_topic=True, failed=True))
            continue
        try:
            stance = float(item.get("stance", 0))
            hostility = float(item.get("hostility", 0))
            confidence = float(item.get("confidence", 0))
            off_topic = bool(item.get("off_topic", True))
        except (ValueError, TypeError):
            results.append(StanceResult(title, 0.0, 0.0, 0.0,
                                        off_topic=True, failed=True))
            continue
        if not all(np.isfinite([stance, hostility, confidence])):
            results.append(StanceResult(title, 0.0, 0.0, 0.0,
                                        off_topic=True, failed=True))
            continue

        # Limitar a rangos válidos
        stance = max(-1.0, min(1.0, stance))
        hostility = max(0.0, min(1.0, hostility))
        confidence = max(0.0, min(1.0, confidence))

        results.append(StanceResult(
            title=title,
            stance=stance,
            hostility=hostility,
            confidence=confidence,
            off_topic=off_topic,
        ))

    return results
