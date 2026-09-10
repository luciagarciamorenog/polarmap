"""
Ingesta desde GDELT DOC 2.0 API.

Por qué GDELT como columna vertebral:
  - Gratis, sin API key, sin aprobación.
  - 65 idiomas, 150+ países, actualización cada 15 minutos.
  - Ya trae país de origen del medio -> resuelve el problema de
    geolocalización que hunde a la mayoría de proyectos de este tipo.

Limitación honesta que hay que documentar en la web:
  GDELT cubre MEDIOS, no conversación social. Mide el discurso público
  publicado, no lo que la gente dice en redes. Es un proxy fuerte pero
  parcial. La capa de redes sociales se añade después (ver README).
"""

from __future__ import annotations

import time
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Países con su código GDELT y el idioma dominante de sus medios.
# El idioma importa: los clasificadores rinden peor fuera del inglés y hay
# que corregirlo, o el mapa mostrará falsamente "tranquilos" a los países
# no anglófonos.
COUNTRIES = {
    "ES": {"gdelt": "Spain",         "lang": "spa", "name": "España"},
    "US": {"gdelt": "UnitedStates",  "lang": "eng", "name": "Estados Unidos"},
    "GB": {"gdelt": "UnitedKingdom", "lang": "eng", "name": "Reino Unido"},
    "FR": {"gdelt": "France",        "lang": "fra", "name": "Francia"},
    "DE": {"gdelt": "Germany",       "lang": "deu", "name": "Alemania"},
    "IT": {"gdelt": "Italy",         "lang": "ita", "name": "Italia"},
    "BR": {"gdelt": "Brazil",        "lang": "por", "name": "Brasil"},
    "AR": {"gdelt": "Argentina",     "lang": "spa", "name": "Argentina"},
    "MX": {"gdelt": "Mexico",        "lang": "spa", "name": "México"},
    "PL": {"gdelt": "Poland",        "lang": "pol", "name": "Polonia"},
    "NL": {"gdelt": "Netherlands",   "lang": "nld", "name": "Países Bajos"},
    "IN": {"gdelt": "India",         "lang": "eng", "name": "India"},
}

# Ejes temáticos. Deliberadamente pocos y estables: la comparabilidad
# entre países y en el tiempo exige que el eje no cambie.
TOPICS = {
    "inmigracion": {
        "spa": '(inmigración OR inmigrantes OR migración OR frontera)',
        "eng": '(immigration OR immigrants OR migrants OR border)',
        "fra": '(immigration OR immigrés OR migrants OR frontière)',
        "deu": '(Einwanderung OR Migranten OR Flüchtlinge OR Grenze)',
        "ita": '(immigrazione OR immigrati OR migranti OR frontiera)',
        "por": '(imigração OR imigrantes OR migrantes OR fronteira)',
        "pol": '(imigracja OR imigranci OR migranci OR granica)',
        "nld": '(immigratie OR immigranten OR migranten OR grens)',
    },
    "clima": {
        "spa": '("cambio climático" OR "emergencia climática" OR "transición energética")',
        "eng": '("climate change" OR "climate crisis" OR "energy transition")',
        "fra": '("changement climatique" OR "crise climatique")',
        "deu": '(Klimawandel OR Klimakrise OR Energiewende)',
        "ita": '("cambiamento climatico" OR "crisi climatica")',
        "por": '("mudança climática" OR "crise climática")',
        "pol": '("zmiany klimatu" OR "kryzys klimatyczny")',
        "nld": '(klimaatverandering OR klimaatcrisis)',
    },
    "genero": {
        "spa": '(feminismo OR "violencia de género" OR "igualdad de género")',
        "eng": '(feminism OR "gender equality" OR "gender violence")',
        "fra": '(féminisme OR "égalité des sexes")',
        "deu": '(Feminismus OR Gleichberechtigung OR Gendern)',
        "ita": '(femminismo OR "parità di genere")',
        "por": '(feminismo OR "igualdade de gênero")',
        "pol": '(feminizm OR "równość płci")',
        "nld": '(feminisme OR gendergelijkheid)',
    },
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


class GDELTClient:
    def __init__(self, timeout: int = 30, max_retries: int = 3, pause: float = 5.0):
        self.timeout = timeout
        self.max_retries = max_retries
        self.pause = pause  # GDELT no publica rate limit, pero castiga ráfagas
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "polarmap/0.1 (research; contact@example.org)"
        })

    def _get(self, params: dict) -> dict | None:
        for attempt in range(self.max_retries):
            try:
                r = self.session.get(GDELT_DOC_API, params=params, timeout=self.timeout)
                if r.status_code == 429 or "limit requests" in r.text[:200]:
                    wait = self.pause * (2 ** attempt)
                    log.warning("rate limited, esperando %.1fs", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                if not r.text.strip():
                    return None
                return r.json()
            except (requests.RequestException, json.JSONDecodeError) as e:
                log.warning("intento %d fallido: %s", attempt + 1, e)
                time.sleep(self.pause * (2 ** attempt))
        return None

    def fetch(
        self,
        country: str,
        topic: str,
        hours_back: int = 24,
        max_records: int = 250,
    ) -> list[Article]:
        """
        Descarga artículos de un país sobre un tema en la ventana temporal.

        max_records tope de GDELT = 250 por llamada. Para volúmenes mayores
        hay que trocear la ventana temporal (ver fetch_window).
        """
        if country not in COUNTRIES:
            raise ValueError(f"país no configurado: {country}")
        meta = COUNTRIES[country]
        lang = meta["lang"]

        query_terms = TOPICS.get(topic, {}).get(lang)
        if not query_terms:
            log.info("sin query para %s/%s, se omite", topic, lang)
            return []

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours_back)

        params = {
            "query": f'{query_terms} sourcecountry:{meta["gdelt"]}',
            "mode": "artlist",
            "format": "json",
            "maxrecords": min(max_records, 250),
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
            "sort": "datedesc",
        }

        data = self._get(params)
        time.sleep(self.pause)
        if not data or "articles" not in data:
            return []

        out = []
        for a in data["articles"]:
            out.append(Article(
                url=a.get("url", ""),
                title=a.get("title", ""),
                domain=a.get("domain", ""),
                country=country,
                language=lang,
                seendate=a.get("seendate", ""),
                topic=topic,
            ))
        return out

    def fetch_window(
        self,
        country: str,
        topic: str,
        hours_back: int = 168,
        chunk_hours: int = 12,
    ) -> list[Article]:
        """
        Trocea una ventana larga en bloques para superar el tope de 250
        registros por llamada. Necesario para tener N suficiente por
        país-tema: sin N, el índice no es estable.
        """
        all_articles: dict[str, Article] = {}
        chunks = max(1, hours_back // chunk_hours)
        for i in range(chunks):
            end_offset = i * chunk_hours
            arts = self._fetch_offset(country, topic, end_offset, chunk_hours)
            for a in arts:
                all_articles[a.url] = a  # dedup por URL
        return list(all_articles.values())

    def _fetch_offset(self, country, topic, end_offset_hours, span_hours):
        meta = COUNTRIES[country]
        lang = meta["lang"]
        query_terms = TOPICS.get(topic, {}).get(lang)
        if not query_terms:
            return []

        end = datetime.now(timezone.utc) - timedelta(hours=end_offset_hours)
        start = end - timedelta(hours=span_hours)
        params = {
            "query": f'{query_terms} sourcecountry:{meta["gdelt"]}',
            "mode": "artlist",
            "format": "json",
            "maxrecords": 250,
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
            "sort": "datedesc",
        }
        data = self._get(params)
        time.sleep(self.pause)
        if not data or "articles" not in data:
            return []
        return [Article(
            url=a.get("url", ""), title=a.get("title", ""),
            domain=a.get("domain", ""), country=country, language=lang,
            seendate=a.get("seendate", ""), topic=topic,
        ) for a in data["articles"]]


def source_concentration(articles: list[Article]) -> dict:
    """
    Control anti-sesgo: si el 80% de los artículos de un país vienen de dos
    dominios, no estás midiendo el discurso de ese país, estás midiendo la
    línea editorial de dos periódicos. Hay que reportarlo, y si es extremo,
    marcar el dato como no fiable en el mapa.
    """
    from collections import Counter
    if not articles:
        return {"n": 0, "n_domains": 0, "hhi": float("nan"), "top_share": float("nan")}
    counts = Counter(a.domain for a in articles)
    total = sum(counts.values())
    shares = [c / total for c in counts.values()]
    hhi = sum(s ** 2 for s in shares)  # Herfindahl
    return {
        "n": total,
        "n_domains": len(counts),
        "hhi": float(hhi),
        "top_share": float(max(shares)),
        "top_domains": counts.most_common(5),
        "reliable": bool(len(counts) >= 8 and hhi < 0.25),
    }
