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
    source_concentration, Article, CACHE_DIR, is_soft_section, title_key,
    COL_DATE, COL_SOURCE_NAME, COL_DOC_ID, COL_THEMES, COL_ENHANCED_THEMES,
    COL_TONE,
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


# Un tema cuenta para un artículo solo si GKG lo detecta al menos estas
# veces en el texto. Con una sola mención, el 60-75 % de los artículos
# de un grupo hablaba de otra cosa (medido en ES, 23-sep-2026).
MIN_MENTIONS = 2


def theme_mentions(row: list[str]) -> Counter:
    """
    Menciones por tema en un artículo, sin etiquetas de ruido.

    Lee V2EnhancedThemes (una entrada por mención). Si viene vacío,
    cae a V1Themes contando 1 por tema.
    """
    field = row[COL_ENHANCED_THEMES] if len(row) > COL_ENHANCED_THEMES else ""
    if not field:
        return Counter(extract_themes(row[COL_THEMES]))
    counts = Counter()
    for part in field.split(";"):
        name = part.split(",")[0].strip().upper()
        if name and not is_noise_theme(name):
            counts[name] += 1
    return counts


@dataclass
class AdmitStats:
    """Qué se descarta antes de analizar, para poder reportarlo."""
    seen: int = 0
    no_title: int = 0
    untitled: int = 0   # sin titular en la URL, admitidos a la espera del de la página
    soft: int = 0
    duplicate: int = 0
    admitted: int = 0


def _admit(row: list[str], cset: set[str],
           seen_keys: dict[str, set[str]], stats: AdmitStats,
           allow_untitled: bool = False,
           ) -> tuple[str, str] | None:
    """
    Filtro común a censo y extracción: país pedido, titular
    reconstruible, sección no blanda y pieza no sindicada ya vista.
    Devuelve (country, title) o None.

    allow_untitled: si la URL no trae un titular legible (El Mundo usa un
    id, RTVE y La Voz de Galicia ponen un número al final), se admite con
    titular vacío para leer el real de la página después. Sin esto se
    perdían medios enteros. Quien lo active debe tirar luego los que sigan
    sin titular (pipeline_discover.enrich_with_leads lo hace).
    """
    if len(row) <= COL_TONE:
        return None
    country = domain_to_country(row[COL_SOURCE_NAME])
    if country is None or country not in cset:
        return None
    stats.seen += 1
    url = row[COL_DOC_ID]
    title = slug_to_title(url)
    if not title and not allow_untitled:
        stats.no_title += 1
        return None
    if is_soft_section(url):
        stats.soft += 1
        return None
    if not title:
        stats.untitled += 1
    key = title_key(title) if title else "url:" + url
    if key in seen_keys[country]:
        stats.duplicate += 1
        return None
    seen_keys[country].add(key)
    stats.admitted += 1
    return country, title


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


def _census_one_zip(raw: bytes, countries: set[str],
                    seen_keys: dict[str, set[str]], stats: AdmitStats,
                    ) -> tuple[dict, dict, int, int]:
    """
    Lee UN fichero GKG y cuenta temas por país. NO guarda titulares.
    Solo cuenta artículos que pasan _admit y temas con saliencia
    (>= MIN_MENTIONS), para que el censo mida lo mismo que se analiza.

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
        admitted = _admit(row, countries, seen_keys, stats)
        if admitted is None:
            continue
        country, _ = admitted
        n_country += 1

        themes = [t for t, m in theme_mentions(row).items()
                  if m >= MIN_MENTIONS]
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
    seen_keys: dict[str, set[str]] = defaultdict(set)
    stats = AdmitStats()

    for i, stamp in enumerate(stamps, 1):
        # Recordatorio: GDELT publica DOS series por intervalo:
        #   - Principal (medios en inglés)
        #   - Translation (resto de idiomas: español, francés, alemán...)
        # HAY QUE BAJAR LAS DOS o se pierden cientos de artículos.
        for is_trans in (False, True):
            raw = download_gkg(stamp, cache_dir, translation=is_trans)
            if raw is None:
                continue
            counts, cooccur, nr, nc = _census_one_zip(raw, cset,
                                                      seen_keys, stats)
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

    log.info("  censo: %d artículos de los países pedidos → %d admitidos "
             "(sin titular %d, sección blanda %d, sindicados %d)",
             stats.seen, stats.admitted, stats.no_title, stats.soft,
             stats.duplicate)

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

    # Dedup por titular (cubre la misma URL y la pieza sindicada)
    seen_keys: dict[str, set[str]] = defaultdict(set)
    stats = AdmitStats()
    n_low_salience = 0
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
                country = domain_to_country(row[COL_SOURCE_NAME])
                if country is None or country not in cset:
                    continue

                # ¿El grupo es central en el artículo, no una mención
                # de pasada? (antes de _admit, para no marcar como visto
                # un titular que este grupo no usa)
                mentions = theme_mentions(row)
                salience = sum(mentions[t] for t in tags_upper)
                if salience == 0:
                    continue
                if salience < MIN_MENTIONS:
                    n_low_salience += 1
                    continue

                admitted = _admit(row, cset, seen_keys, stats)
                if admitted is None:
                    continue
                _, title = admitted
                url = row[COL_DOC_ID]
                domain = row[COL_SOURCE_NAME]

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

    log.info("  extracción: %d admitidos | descartados: %d mención de "
             "pasada, %d sin titular, %d sección blanda, %d sindicados",
             stats.admitted, n_low_salience, stats.no_title, stats.soft,
             stats.duplicate)
    return dict(by_country)


def fetch_country_headlines(countries: list[str], hours_back: int = 24,
                            every_n: int = 1, max_files: int | None = None,
                            cache_dir: Path = CACHE_DIR,
                            allow_untitled: bool = False,
                            ) -> tuple[dict[str, list[Article]], AdmitStats]:
    """
    Todos los titulares analizables de cada país en la ventana, sin
    filtrar por etiqueta GKG: el tema lo decide después assign_topics
    leyendo el titular. Solo aplica _admit (titular, sección, sindicación).

    max_files=None lee toda la ventana. (Antes el tope era 60 ficheros:
    con every_n=1 y 24 h hay 96, y se habrían perdido 9 horas.)
    allow_untitled: ver _admit.
    """
    stamps = gkg_timestamps(hours_back, every_n)[:max_files]
    cset = set(countries)
    seen_keys: dict[str, set[str]] = defaultdict(set)
    stats = AdmitStats()
    by_country: dict[str, list[Article]] = defaultdict(list)
    csv.field_size_limit(10 * 1024 * 1024)

    for i, stamp in enumerate(stamps, 1):
        for is_trans in (False, True):
            raw = download_gkg(stamp, cache_dir, translation=is_trans)
            if raw is None:
                continue
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
                data = zf.read(zf.namelist()[0]).decode("utf-8",
                                                        errors="replace")
            except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
                continue
            reader = csv.reader(io.StringIO(data), delimiter="\t",
                                quoting=csv.QUOTE_NONE)
            for row in reader:
                admitted = _admit(row, cset, seen_keys, stats,
                                  allow_untitled=allow_untitled)
                if admitted is None:
                    continue
                country, title = admitted
                tone = None
                try:
                    tone = float(row[COL_TONE].split(",")[0])
                except (ValueError, IndexError):
                    pass
                by_country[country].append(Article(
                    url=row[COL_DOC_ID], title=title,
                    domain=row[COL_SOURCE_NAME], country=country,
                    language="", seendate=row[COL_DATE], topic="",
                    tone=tone,
                ))
        if i % 6 == 0 or i == len(stamps):
            log.info("  titulares: %d/%d ficheros | %d admitidos",
                     i, len(stamps), stats.admitted)

    log.info("  titulares: %d de los países pedidos → %d admitidos "
             "(sin titular %d, sección blanda %d, sindicados %d; "
             "%d sin titular en la URL, pendientes del de la página)",
             stats.seen, stats.admitted, stats.no_title, stats.soft,
             stats.duplicate, stats.untitled)
    return dict(by_country), stats


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


def _call_api_message(body: dict, api_key: str, timeout: int) -> dict:
    """POST a Messages con reintentos. Devuelve el mensaje completo
    (contenido y `usage`, que analysis/runner.py registra)."""
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
            message = resp.json()
            message["content"][0]["text"]          # debe traer texto
            return message
        except (ValueError, KeyError, IndexError, TypeError) as e:
            last = f"respuesta sin texto: {e}"
    raise TransientAPIError(
        f"sin respuesta válida tras {API_RETRIES} intentos ({last})")


def _call_api(body: dict, api_key: str, timeout: int) -> str:
    """Como _call_api_message, pero devuelve solo el texto."""
    return _call_api_message(body, api_key, timeout)["content"][0]["text"].strip()


def _cached_system(text: str) -> list[dict]:
    """Bloque de sistema cacheable: la parte fija que se repite en cada
    lote de un mismo debate o país. Por debajo del mínimo del modelo
    (1.024 tokens en Sonnet 4.6, 4.096 en Haiku 4.5) no se cachea, sin
    error; runner.LEDGER lo deja ver en cache_write/cache_read."""
    return [{"type": "text", "text": text,
             "cache_control": {"type": "ephemeral"}}]


def _require_key(api_key: str | None = None):
    if api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Falta ANTHROPIC_API_KEY")


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


def _json_array(text: str):
    """Primer array JSON de la respuesta (tolera ```json y texto extra)."""
    cleaned = re.sub(r"```(json)?", "", text).strip()
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return items if isinstance(items, list) else None


# ===================================================================
# DEBATES DEL DÍA Y ASIGNACIÓN DE TITULARES
# ===================================================================
# El tema se decide leyendo el titular, no por la etiqueta GKG: las
# etiquetas se ponen aunque el tema salga una vez en el cuerpo, y los
# grupos resultantes eran 65-88 % off-topic (ES, 23-sep-2026).
#
#   1. discover_debates: Claude lee TODOS los titulares del país y
#      propone los debates públicos del día, cada uno con dos polos.
#   2. assign_topics: un modelo barato asigna cada titular a un debate
#      o a ninguno. Un titular cuenta como mucho en un debate.
# ===================================================================

ASSIGN_MODEL = "claude-haiku-4-5"
ASSIGN_BATCH = 50
MAX_DISCOVER_TITLES = 1200
MAX_DEBATES = 6

DISCOVER_INSTRUCTIONS = 'Identifica entre 3 y {max_debates} ASUNTOS DE DEBATE PÚBLICO presentes en estos titulares. Un asunto de debate es una cuestión de fondo en la que actores reales del país (partidos, colectivos, medios, expertos) defienden posturas enfrentadas.\n\nASUNTOS, NO SUCESOS. Los titulares hablan de hechos concretos del día; tu trabajo es agruparlos en el asunto de fondo que los conecta:\n- "Crisis en Ceuta", "llegada de pateras a Canarias" y "ley de nietos" → asunto "Política migratoria".\n- "Desahucio de una vecina", "precio del alquiler" y "ayudas a la compra" → asunto "Vivienda".\n- "Juicio a un cargo público", "amnistía" y "choque Gobierno-jueces" → asunto "Justicia e instituciones".\nSi dos asuntos candidatos comparten actores o se enmarcan igual, fúndelos en uno. Prefiere 4 asuntos que agrupen muchos titulares a 8 que agrupen pocos.\n\nPara cada asunto:\n- label: nombre corto (2-5 palabras) en el idioma principal del país. Debe servir igual otro día: sin nombres propios de personas ni fechas.\n- pole_a y pole_b: las dos posturas enfrentadas sobre QUÉ DEBE HACERSE con el asunto, cada una en una frase. Son políticas o soluciones que alguien defiende, no valoraciones de personas.\n- NUNCA polos que juzguen a un político, un partido o su actuación concreta (quién ganó un debate, quién mintió, quién recurrió a la bronca). Eso no es un debate de fondo y hace que el eje dependa de a qué partido perjudica cada noticia.\n- Campañas electorales, encuestas, pactos y la gestión del Gobierno en general: si forman un asunto, llámalo "Continuidad o cambio de Gobierno" y usa exactamente estos polos: pole_a "El Gobierno actual debe continuar" y pole_b "Hace falta un cambio de Gobierno". Las propuestas concretas de campaña (pensiones, vivienda, inmigración) van a su asunto de política, no a este.\n- includes: 3-6 sucesos o subtemas concretos de estos titulares que caen en el asunto.\n\nCriterios:\n- Cada asunto debe cubrir MUCHOS titulares (idealmente 40 o más). No inventes asuntos para llegar al máximo.\n- Que no se solapen: cada titular debería encajar como mucho en uno.\n- UN SOLO OBJETO POR ASUNTO. No juntes dos cuestiones en un eje (por ejemplo, «independencia judicial» y «corrupción» son dos asuntos: una noticia sobre un caso de corrupción no dice nada sobre si los jueces son independientes). Si un grupo de titulares mezcla dos cuestiones, elige la que más titulares puedan situar o créalas por separado.\n- Cada titular del asunto debe poder situarse en el eje por lo que cuenta. Si para situarlo hubiera que saber a qué partido perjudica, el eje está mal planteado.\n- Los dos polos hablan del MISMO objeto que el label y son posiciones que alguien defiende de verdad. No metas en un polo un asunto distinto (por ejemplo, no conviertas un debate sanitario en uno migratorio).\n- Redacción neutral y simétrica: ninguno de los polos debe sonar como el razonable.\n- Excluye sucesos, deportes, cultura y noticias de servicio que no tengan controversia.\n\nResponde SOLO con un array JSON, ordenado de más a menos titulares:\n[{{"label": "...", "pole_a": "...", "pole_b": "...", "includes": ["...", "..."]}}]\nSi no hay ningún debate, responde [].'


def build_discover_body(titles: list[str], country_name: str,
                        max_debates: int = MAX_DEBATES,
                        known: list[str] | None = None) -> dict:
    sample = titles
    if len(sample) > MAX_DISCOVER_TITLES:
        # Muestra sistemática, no aleatoria: reproducible entre corridas.
        step = len(sample) / MAX_DISCOVER_TITLES
        sample = [sample[int(i * step)] for i in range(MAX_DISCOVER_TITLES)]
    block = "\n".join(f"- {t}" for t in sample)
    prompt = (f"Estos son {len(sample)} titulares de medios de {country_name} de "
              "las últimas 24 horas. Algunos están reconstruidos desde la URL: sin "
              f"tildes y a veces sin artículos.\n\n{block}\n\n"
              + DISCOVER_INSTRUCTIONS.format(max_debates=max_debates)
              + _known_block(known))
    return {
        "model": DISCOVER_MODEL,
        "max_tokens": 2000,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }


def _known_block(known: list[str] | None) -> str:
    """Temas fijos del país: si el asunto del día es uno de ellos, se usa
    exactamente su nombre, para que los días se puedan comparar."""
    if not known:
        return ""
    lines = "\n".join(f"- {k}" for k in known)
    return ("\n\nTEMAS FIJOS. Estos asuntos ya están definidos con sus polos:\n"
            f"{lines}\n"
            "Si un asunto de hoy trata de uno de ellos, usa EXACTAMENTE ese label "
            "(sus polos se fijarán solos: escribe los tuyos igualmente). Crea un "
            "label nuevo solo si el asunto no encaja en ninguno.")


def parse_debates(text: str, max_debates: int = MAX_DEBATES) -> list[dict]:
    """Debates de la respuesta. Ilegible → TransientAPIError (nunca "no hay
    debate"); lista vacía → no hay debates."""
    items = _json_array(text or "")
    if items is None:
        raise TransientAPIError(
            f"respuesta de debates ilegible: {(text or '')[:200]!r}")
    debates, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label", "")).strip()
        pole_a = str(it.get("pole_a", "")).strip()
        pole_b = str(it.get("pole_b", "")).strip()
        includes = it.get("includes") or []
        includes = [str(x).strip() for x in includes
                    if isinstance(x, str) and x.strip()][:6]
        norm = " ".join(label.lower().split())
        if not (label and pole_a and pole_b) or norm in seen:
            continue
        seen.add(norm)
        debates.append({"label": label, "pole_a": pole_a, "pole_b": pole_b,
                        "includes": includes})
    return debates[:max_debates]


def discover_debates(titles: list[str], country_name: str,
                     api_key: str | None = None,
                     max_debates: int = MAX_DEBATES) -> list[dict]:
    """
    Debates públicos presentes en los titulares de un país.

    Devuelve [{"label", "pole_a", "pole_b", "includes"}, ...] (puede
    ser []). `includes` son los sucesos del día que caen en ese asunto;
    se pasan a la asignación para que no se queden en "ninguno".
    Un fallo de la API o una respuesta ilegible lanza TransientAPIError;
    una key inválida, FatalAPIError. Nunca se confunde con "no hay debate".
    """
    from analysis import runner
    _require_key(api_key)
    (text,) = runner.run([build_discover_body(titles, country_name, max_debates)],
                         "debates", "sync")
    if text is None:
        raise TransientAPIError("la API no devolvió los debates")
    return parse_debates(text, max_debates)


# Resultado de assign_topics por titular:
ASSIGN_NONE = 0      # no pertenece a ningún debate
ASSIGN_FAILED = -1   # no se pudo asignar (API o respuesta ilegible)


# Regla de la campaña (codebook v5, 26 nov 2026). Solo se añade si entre los
# debates del día está el eje fijo del Gobierno.
GOV_LABEL = "Continuidad o cambio de Gobierno"
CAMPAIGN_RULE = False   # en pruebas (validate/test_assign_rule.py); la corrida diaria no la usa aún


def _campaign_rules(k_gov: int) -> str:
    return (
        f"- Elecciones y campaña: toda noticia de las elecciones o de la campaña "
        f"(candidatos, mítines, debates electorales, encuestas, reparto de escaños, "
        f"mesas, voto, sueldos de diputados, pactos de investidura) o de la gestión "
        f"del Gobierno en general va al debate {k_gov} ({GOV_LABEL}), aunque solo "
        f"informe y no tome postura.\n"
        f"- Excepción: si la noticia de campaña trata DE FONDO una política que tiene "
        f"su propio debate en la lista (por ejemplo, una propuesta o medida concreta "
        f"sobre vivienda o sobre inmigración), va a ese debate. Una mención de pasada "
        f"dentro de una crónica general de campaña no cuenta: esa crónica va al debate "
        f"{k_gov}.\n")


def build_assign_bodies(texts: list[str], debates: list[dict],
                        campaign_rule: bool | None = None
                        ) -> list[tuple[dict, int]]:
    """Una petición por cada ASSIGN_BATCH textos. La lista de debates va
    en el bloque de sistema cacheable: es igual en todos los lotes del país."""
    if campaign_rule is None:
        campaign_rule = CAMPAIGN_RULE
    k_gov = next((k for k, d in enumerate(debates, 1) if d["label"] == GOV_LABEL), None)
    extra_rules = _campaign_rules(k_gov) if (campaign_rule and k_gov) else ""
    def describe(k, d):
        line = f"{k}. {d['label']} (de «{d['pole_a']}» a «{d['pole_b']}»)"
        if d.get("includes"):
            line += "\n   incluye, entre otros: " + "; ".join(d["includes"])
        return line
    topics = "\n".join(describe(k, d) for k, d in enumerate(debates, 1))
    system = f"""Asignas cada noticia al debate público del que trata.

DEBATES:
{topics}

Reglas:
- topic = número del debate si la noticia trata de su asunto, aunque sea de forma informativa y sin tomar postura. Cada debate es un asunto amplio: cuenta cualquier hecho del día que forme parte de él, no solo los ejemplos listados.
- topic = 0 si no trata de ninguno. Ante la duda, 0: es mejor perder una noticia que meter una que no toca.
- Si encaja en varios, el más central.
{extra_rules}
Responde SOLO con un array JSON, un elemento por noticia:
[{{"id": 1, "topic": 0}}]"""
    bodies = []
    for i in range(0, len(texts), ASSIGN_BATCH):
        chunk = texts[i:i + ASSIGN_BATCH]
        numbered = "\n".join(f"{j}. {t}" for j, t in enumerate(chunk, 1))
        bodies.append(({
            "model": ASSIGN_MODEL,
            "max_tokens": 2000,
            "temperature": 0,
            "system": _cached_system(system),
            "messages": [{"role": "user", "content":
                          "NOTICIAS (titular y, si hay, entradilla; algunas "
                          f"reconstruidas desde la URL, sin tildes):\n{numbered}"}],
        }, len(chunk)))
    return bodies


def parse_assign(texts: list[str | None], bodies: list[tuple[dict, int]],
                 n_debates: int) -> list[int]:
    out: list[int] = []
    for (_, n), text in zip(bodies, texts):
        out.extend([ASSIGN_FAILED] * n if text is None
                   else _parse_assign_response(text, n, n_debates))
    return out


def assign_topics(titles: list[str], debates: list[dict],
                  api_key: str | None = None) -> list[int]:
    """
    Asigna cada titular a un debate (1..len(debates)), a ninguno
    (ASSIGN_NONE) o marca que falló (ASSIGN_FAILED). Mismo orden que
    `titles`. FatalAPIError corta la corrida.
    """
    from analysis import runner
    _require_key(api_key)
    bodies = build_assign_bodies(titles, debates)
    texts = runner.run([b for b, _ in bodies], "asignacion", "sync")
    return parse_assign(texts, bodies, len(debates))


def _parse_assign_response(text: str, n_titles: int,
                           n_debates: int) -> list[int]:
    items = _json_array(text)
    if items is None:
        log.error("respuesta de asignación ilegible")
        return [ASSIGN_FAILED] * n_titles
    by_id = {it.get("id"): it.get("topic") for it in items
             if isinstance(it, dict)}
    out = []
    for j in range(1, n_titles + 1):
        topic = by_id.get(j)
        if isinstance(topic, bool) or not isinstance(topic, int) \
                or not 0 <= topic <= n_debates:
            out.append(ASSIGN_FAILED)   # ausente o fuera de rango
        else:
            out.append(topic)
    return out


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


US_THEM = ("no", "political", "social_groups", "nonpolitical_rivals")


@dataclass
class StanceResult:
    """Clasificación de un titular (libro de códigos v2, validate/CODEBOOK.md)."""
    title: str
    stance: float        # -1 (polo A) a +1 (polo B), framing incluido
    hostility: float     # 0 (neutro) a 1 (hostil): SOLO el lenguaje
    confidence: float    # 0 a 1
    off_topic: bool      # True si el titular no va del tema
    failed: bool = False  # True si NO se pudo clasificar (API o parseo).
                          # No es off-topic: es un dato perdido.
    conflict: float = float("nan")  # 0 a 1: conflictividad de la historia
    us_them: str | None = None      # uno de US_THEM
    bloc: float = float("nan")      # -1 favorable al Gobierno ... +1 a la oposición
    why: str = ""                   # motivo breve que da el modelo antes de puntuar
    stance_sd: float = float("nan") # desviación entre pasadas (si hay varias)
    speaker: str | None = None      # v4: de quién son las palabras hostiles ("medio" o nombre)
    speaker_org: str | None = None  # v4: partido u organización de esa persona
    target: str | None = None       # v4: a qué apunta la hostilidad (uno de TARGETS)


CLASSIFY_SYSTEM = """Eres un analista de medios. Codificas noticias para medir polarización en varias dimensiones distintas. No las mezcles.

EJE DEL DEBATE:
  Polo A (-1): {pole_a}
  Polo B (+1): {pole_b}

Cada noticia viene como «titular. entradilla» (a veces solo el titular).

0. why (máx. 15 palabras), ANTES de puntuar: qué hecho, cita o ángulo elige el medio y a qué polo favorece esa elección. Si de verdad no favorece a ninguno, dilo.

1. stance (-1.0 a +1.0): dónde queda la noticia entre el polo A y el polo B.
   Cuenta la postura EXPLÍCITA y también la IMPLÍCITA por framing: qué hecho elige contar el medio, desde qué ángulo y con qué palabras.
   - ±1.0: defiende un polo, o framing muy cargado (términos de un bando, una consigna como titular).
   - ±0.5: el ángulo o la selección del hecho favorecen un polo sin afirmarlo.
   - 0.0: SOLO para un dato o trámite sin ángulo, o una noticia que recoge a ambos bandos por igual. Que el tono sea sobrio no la hace neutral: un titular informativo que elige un hecho que solo le conviene a un bando no es 0.
   Ejemplos (eje «intervenir en vivienda» vs «mercado»): «"Maricarmen somos todas": Tenerife alza la voz contra los desahucios» → -1.0; «Valladolid contabiliza un desahucio cada dos días» → -0.5 (elige el drama de los desahucios); «Los propietarios tardan 20 meses en recuperar una vivienda okupada» → +0.5 (elige el problema del propietario); «La compraventa cae un 5,1 % en julio» → 0.0.
   Sitúa la noticia por lo que dice del asunto del eje, no por a qué partido perjudica (eso va en bloc).

2. hostility (0.0 a 1.0): SOLO la agresividad del lenguaje (titular, entradilla y citas que el medio destaca). No la gravedad del hecho.
   1.0 insulto, burla, deshumanización («a gorrazos», «invasión», «cloacas»); 0.5 léxico combativo («choque», «maniobra», «fondo buitre», «acoso»); 0.0 lenguaje neutro aunque el hecho sea grave.

3. conflict (0.0 a 1.0): cuán conflictiva o dañina es la historia tal como se cuenta, independientemente de las palabras.
   1.0 violencia, víctimas graves, crisis abierta, confrontación política directa; 0.5 disputa, crítica, queja, daño o riesgo limitado; 0.0 dato, servicio, logro, agenda.

4. us_them: si la noticia presenta DOS partes identificables enfrentadas.
   "political" (partidos, Gobierno, oposición, instituciones), "social_groups" (colectivos sociales), "nonpolitical_rivals" (clubes, empresas frente a clientes, asociaciones) o "no". No basta con una víctima o un culpable sin el otro bando.

5. bloc (-1.0 a +1.0, o null): a qué bloque político nacional deja mejor o peor la noticia, sea cual sea el debate.
   -1.0 favorece claramente al Gobierno del país o daña a la oposición; +1.0 favorece claramente a la oposición o daña al Gobierno; ±0.5 de forma leve; 0.0 equilibrada. null si la noticia no implica a actores políticos nacionales.

6. confidence (0.0 a 1.0): tu confianza en stance.

7. off_topic: true SOLO si la noticia trata de algo completamente distinto del debate. Si trata del asunto aunque sea informativa, off_topic=false.

8. speaker (SIEMPRE, haya hostilidad o no): quién habla en la noticia.
   Si el titular o la entradilla recoge palabras o una postura de alguien (cita directa o indirecta: «Abascal: ...», «Sánchez acusa a ...»), el nombre de esa persona tal como se le conoce públicamente, nombre y primer apellido («Santiago Abascal», «Pedro Sánchez», «Isabel Díaz Ayuso»), o el de la organización si habla como tal («PP», «Sindicato de Inquilinas»). Si hay varias, la principal (si hay hostilidad, la de las palabras más hostiles). Si nadie habla y es el medio quien cuenta o valora, "medio".
   speaker_org: siglas del partido u organización de esa persona si las conoces con seguridad («PP», «PSOE», «Vox», «Sumar», «Podemos», «ERC», «Junts», «Gobierno»…); null si no, o si speaker es "medio".

9. target (solo si hostility > 0; si no, null): contra qué va la hostilidad.
   "ideas" si critica una medida, una postura o una gestión; "personas" si descalifica, ridiculiza o insulta a personas concretas; "grupo" si descalifica a un colectivo entero (migrantes, jueces, okupas, los votantes de un partido).

Responde SOLO con un array JSON, un objeto por noticia, con why en primer lugar:
[{{"id":1,"why":"elige el drama de un desahucio: favorece intervenir","stance":-0.5,"hostility":0.0,"conflict":0.5,"us_them":"no","bloc":null,"confidence":0.8,"off_topic":false,"speaker":"medio","speaker_org":null,"target":null}},
 {{"id":2,"why":"cita que llama tirano al presidente: favorece el cambio","stance":0.5,"hostility":1.0,"conflict":1.0,"us_them":"political","bloc":1.0,"confidence":0.8,"off_topic":false,"speaker":"Santiago Abascal","speaker_org":"Vox","target":"personas"}}]"""


def build_classify_bodies(texts: list[str], pole_a: str, pole_b: str,
                          temperature: float = 0.1,
                          ) -> list[tuple[dict, list[str]]]:
    """Una petición por cada BATCH_SIZE textos. El libro de códigos y el
    eje van en el bloque de sistema cacheable: iguales en todo el debate."""
    system = CLASSIFY_SYSTEM.format(pole_a=pole_a, pole_b=pole_b)
    bodies = []
    for i in range(0, len(texts), BATCH_SIZE):
        chunk = texts[i:i + BATCH_SIZE]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(chunk))
        bodies.append(({
            "model": CLASSIFY_MODEL,
            "max_tokens": 4500,
            "temperature": temperature,
            "system": _cached_system(system),
            "messages": [{"role": "user", "content": f"NOTICIAS:\n{numbered}"}],
        }, chunk))
    return bodies


def parse_classify(texts: list[str | None],
                   bodies: list[tuple[dict, list[str]]]) -> list[StanceResult]:
    out: list[StanceResult] = []
    for (_, chunk), text in zip(bodies, texts):
        if text is None:
            log.error("lote de %d titulares SIN clasificar", len(chunk))
            out.extend(_failed(chunk))
        else:
            out.extend(_parse_classify_response(text, chunk))
    return out


def classify_stances(titles: list[str], pole_a: str, pole_b: str,
                     language: str = "spa",
                     api_key: str | None = None
                     ) -> list[StanceResult]:
    """
    Clasifica cada texto (titular, o titular + entradilla) en el eje
    pole_a ↔ pole_b con el libro de códigos v2 (validate/CODEBOOK.md):
    postura con framing, hostilidad del lenguaje, conflictividad,
    nosotros/ellos, confianza y off_topic.

    Un lote que falla tras los reintentos vuelve con failed=True (no
    off_topic). Una key inválida lanza FatalAPIError y corta todo.
    """
    from analysis import runner
    _require_key(api_key)
    bodies = build_classify_bodies(titles, pole_a, pole_b)
    return parse_classify(runner.run([b for b, _ in bodies], "postura", "sync"), bodies)


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

        # Campos v2: si faltan quedan como no medidos (nan / None), sin
        # invalidar la postura, que es lo que alimenta el índice ideológico.
        try:
            conflict = float(item.get("conflict"))
            conflict = max(0.0, min(1.0, conflict)) if np.isfinite(conflict) else float("nan")
        except (TypeError, ValueError):
            conflict = float("nan")
        us_them = item.get("us_them")
        us_them = us_them if us_them in US_THEM else None
        try:
            bloc = item.get("bloc")
            bloc = float(bloc) if bloc is not None else float("nan")
            bloc = max(-1.0, min(1.0, bloc)) if np.isfinite(bloc) else float("nan")
        except (TypeError, ValueError):
            bloc = float("nan")
        why = str(item.get("why") or "")[:200]
        # v4: quién habla se guarda siempre (hace falta el total de sus
        # frases para calcular su nota); contra qué, solo si hay hostilidad.
        speaker = _clean_name(item.get("speaker"))
        if speaker and speaker.lower() in ("medio", "el medio", "periodista"):
            speaker = "medio"
        speaker_org = None if speaker in (None, "medio") else _clean_name(item.get("speaker_org"))
        target = None
        if hostility > 0:
            target = item.get("target") if item.get("target") in TARGETS else None

        results.append(StanceResult(
            title=title,
            stance=stance,
            hostility=hostility,
            confidence=confidence,
            off_topic=off_topic,
            conflict=conflict,
            us_them=us_them,
            bloc=bloc,
            why=why,
            speaker=speaker,
            speaker_org=speaker_org,
            target=target,
        ))

    return results


TARGETS = ("ideas", "personas", "grupo")


def _clean_name(x) -> str | None:
    """Nombre de quien habla: texto corto o None."""
    if not isinstance(x, str):
        return None
    x = " ".join(x.strip().strip("«»\"'").split())
    if x.lower() in ("", "null", "none", "n/a"):
        return None
    return x[:80]


def _mode(values):
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else None


def combine_passes(passes: list[list[StanceResult]]) -> list[StanceResult]:
    """
    Junta varias pasadas de clasificación del mismo titular (mismo orden).
    Postura, hostilidad, conflicto, bloc y confianza: media de las pasadas
    válidas. off_topic: mayoría. stance_sd: desviación de la postura entre
    pasadas, que mide la consistencia del modelo. Un titular solo cuenta
    como fallido si fallan todas las pasadas.
    """
    if len(passes) == 1:
        return passes[0]
    out = []
    for rs in zip(*passes):
        ok = [r for r in rs if not r.failed]
        if not ok:
            out.append(rs[0])
            continue
        on = [r for r in ok if not r.off_topic]
        off_topic = len(on) * 2 < len(ok)
        use = on or ok

        def avg(attr):
            v = np.array([getattr(r, attr) for r in use], dtype=float)
            v = v[np.isfinite(v)]
            return float(v.mean()) if v.size else float("nan")
        st = np.array([r.stance for r in use], dtype=float)
        us = Counter(r.us_them for r in use if r.us_them).most_common(1)
        out.append(StanceResult(
            title=use[0].title, stance=avg("stance"), hostility=avg("hostility"),
            confidence=avg("confidence"), off_topic=off_topic,
            conflict=avg("conflict"), us_them=us[0][0] if us else None,
            bloc=avg("bloc"), why=use[0].why,
            stance_sd=float(st.std()) if st.size > 1 else float("nan"),
            speaker=_mode(r.speaker for r in use),
            speaker_org=_mode(r.speaker_org for r in use),
            target=_mode(r.target for r in use)))
    return out
