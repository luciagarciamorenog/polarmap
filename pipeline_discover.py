"""
Pipeline de polarización por país con debates descubiertos por titular.

POR PAÍS:
  1. Reúne todos los titulares analizables de la ventana (ficheros GKG
     con caché; sin titular, secciones blandas y sindicados fuera) y lee
     de cada página el titular real y la entradilla (ingest/leads.py).
  2. Claude lee los titulares y propone los debates públicos del día,
     cada uno con dos polos.
  3. Un modelo barato asigna cada titular a un debate o a ninguno.
  4. Por debate: clasifica con el libro de códigos v2 (validate/CODEBOOK.md):
     postura con framing, hostilidad del lenguaje, conflictividad y
     «nosotros contra ellos», y calcula métricas de polarización.
  5. Genera snapshot.json con el contrato v1 (CONTRACT.md) para el mapa,
     y el detalle interno en data/internal/.

Uso (la key se lee de .env en la raíz del proyecto; ver .env.example):
    python3 pipeline_discover.py                 # los 10 países v1
    python3 pipeline_discover.py --countries ES --hours 24

    # Solo cuenta titulares (sin gastar API):
    python3 pipeline_discover.py --countries ES --dry-run

Por defecto usa la Batch API (--api-mode batch: mitad de precio, una
tanda por etapa para todos los países). El consumo real de tokens y su
coste quedan en data/internal/<salida>_usage.json.
"""

from __future__ import annotations

import os
import json
import argparse
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from analysis import contract, runner
from analysis.discover import (
    fetch_country_headlines, build_discover_body, parse_debates,
    build_assign_bodies, parse_assign, build_classify_bodies, parse_classify,
    combine_passes,
    source_concentration, TransientAPIError,
    ASSIGN_FAILED, ASSIGN_NONE, MAX_DEBATES,
)
from analysis.metrics import polarization_profile
from ingest.gkg_files import Article, gkg_timestamps
from ingest.leads import fetch_leads
from ingest.gkg_files import title_key
from analysis.ecosystem import ecosystem_report, genre_from_url, summary_lines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pipeline_discover")

OUT_DIR = Path("data")
ENV_PATH = Path(__file__).resolve().parent / ".env"
INTERNAL_DIR = OUT_DIR / "internal"
AXES_PATH = INTERNAL_DIR / "axes.json"

# Si más de este porcentaje de titulares de un tema no se pudo
# clasificar (API o parseo), el tema no vale y la corrida no se publica.
MAX_FAILED_SHARE = 0.10

# Metadatos de países: nombre legible e idioma principal.
# El idioma se usa para que Claude clasifique en el idioma correcto
# y para detectar sesgo lingüístico.
COUNTRY_META = {
    "ES": {"name": "España", "lang": "spa"},
    "US": {"name": "Estados Unidos", "lang": "eng"},
    "GB": {"name": "Reino Unido", "lang": "eng"},
    "FR": {"name": "Francia", "lang": "fra"},
    "DE": {"name": "Alemania", "lang": "deu"},
    "IT": {"name": "Italia", "lang": "ita"},
    "NL": {"name": "Países Bajos", "lang": "nld"},
    "PL": {"name": "Polonia", "lang": "pol"},
    "BR": {"name": "Brasil", "lang": "por"},
    "AR": {"name": "Argentina", "lang": "spa"},
    "MX": {"name": "México", "lang": "spa"},
    "CL": {"name": "Chile", "lang": "spa"},
    "IN": {"name": "India", "lang": "eng"},
}


def enrich_with_leads(headlines: dict[str, list[Article]],
                      respect_tdm: bool = True) -> dict[str, list[Article]]:
    """
    Sustituye el titular de la URL por el real y añade la entradilla.
    Con respect_tdm, los medios que declaran reserva de minería de textos
    (TDMRep) se quedan fuera del análisis. Lo que no se pudo leer sigue
    con el titular de la URL, marcado como title_source="url".

    Las noticias que llegaron sin titular en la URL (El Mundo, RTVE, La
    Voz de Galicia...) se quedan solo si la página da su titular. Al final
    se quitan las copias sindicadas que solo se ven con el titular real.
    """
    urls = [a.url for arts in headlines.values() for a in arts]
    leads = fetch_leads(urls)
    out = {}
    for country, arts in headlines.items():
        kept, n_page, n_tdm, n_untitled, n_dup = [], 0, 0, 0, 0
        seen: set[str] = set()
        for a in arts:
            ld = leads.get(a.url) or {}
            if respect_tdm and ld.get("tdm_reserved"):
                n_tdm += 1
                continue
            if ld.get("status") == "ok" and ld.get("title"):
                a.title, a.title_source = ld["title"], "page"
                a.lead = ld.get("description", "")
                n_page += 1
            if not a.title:
                n_untitled += 1
                continue
            key = title_key(a.title)
            if key in seen:
                n_dup += 1
                continue
            seen.add(key)
            kept.append(a)
        out[country] = kept
        log.info("[%s] titular real + entradilla: %d de %d · fuera por "
                 "reserva TDM: %d · sin titular tampoco en la página: %d · "
                 "sindicados (por titular real): %d",
                 country, n_page, len(arts), n_tdm, n_untitled, n_dup)
    return out


def score_topic(country: str, arts: list[Article], axis: dict,
                stance_results: list, min_articles: int,
                records: dict[str, dict]) -> dict:
    """
    Métricas de UN debate a partir de la clasificación de sus titulares
    (una por titular, en orden). Devuelve el resultado por tema que consume
    contract y anota la clasificación de cada titular en `records`.
    """
    for art, r in zip(arts, stance_results):
        records[art.url].update(
            stance=round(r.stance, 3), hostility=round(r.hostility, 3),
            conflict=(round(r.conflict, 3) if np.isfinite(r.conflict) else None),
            us_them=r.us_them,
            bloc=(round(r.bloc, 3) if np.isfinite(r.bloc) else None),
            why=r.why or None,
            stance_sd=(round(r.stance_sd, 3) if np.isfinite(r.stance_sd) else None),
            confidence=round(r.confidence, 3), off_topic=r.off_topic,
            stance_failed=r.failed)

    n_failed = sum(r.failed for r in stance_results)
    if n_failed > MAX_FAILED_SHARE * len(stance_results):
        log.error("  [%s] %d de %d titulares SIN clasificar (API o "
                  "respuesta ilegible) → tema descartado",
                  country, n_failed, len(stance_results))
        return {
            "status": "classification_failed",
            "axis": axis,
            "n_articles": len(arts),
            "n_failed": n_failed,
        }

    stances, hostilities, confidences, on_topic = [], [], [], []
    conflicts, us_them = [], []
    for art, r in zip(arts, stance_results):
        if r.failed or r.off_topic or not np.isfinite(r.stance):
            continue
        on_topic.append(art)
        if np.isfinite(r.conflict):
            conflicts.append(r.conflict)
        if r.us_them:
            us_them.append(r.us_them)
        stances.append(r.stance)
        hostilities.append(r.hostility)
        confidences.append(r.confidence)

    n_off = len(stance_results) - len(stances) - n_failed
    log.info("  [%s] clasificación: %d on-topic, %d off-topic, "
             "%d fallidos de %d", country, len(stances), n_off,
             n_failed, len(stance_results))
    if stances:
        s, h, c = (np.array(stances), np.array(hostilities),
                   np.array(confidences))
        log.info("  [%s] postura: media=%.2f sd=%.2f | hostil: media=%.2f "
                 "| confianza: media=%.2f",
                 country, s.mean(), s.std(), h.mean(), c.mean())

    if len(stances) < min_articles:
        log.info("  [%s] solo %d clasificaciones útiles → no fiable",
                 country, len(stances))
        return {
            "status": "insufficient_after_filtering",
            "axis": axis,
            "n_articles": len(arts),
            "n_classified": len(stances),
            "n_effective": float(len(stances)),
            "n_failed": n_failed,
        }

    profile = polarization_profile(
        np.array(stances),
        np.array(hostilities),
        weights=np.array(confidences, dtype=float),
        min_n=min_articles,
    )
    conc = source_concentration(on_topic)
    result = {
        "status": "ok",
        "axis": axis,
        "n_articles": len(arts),
        "n_classified": len(stances),
        "n_effective": profile.get("n_effective", 0),
        "n_failed": n_failed,
        "metrics": profile,
        "source_quality": {
            k: conc[k] for k in ("n_domains", "hhi", "top_share", "reliable")
        },
        # Descriptivos de las dimensiones afectiva y narrativa (v2). Aún
        # no entran en el contrato público: se validan antes.
        "descriptives": {
            "hostility_mean": float(np.mean(hostilities)),
            "conflict_mean": float(np.mean(conflicts)) if conflicts else None,
            "us_them_share": (sum(u != "no" for u in us_them) / len(us_them)
                              if us_them else None),
            "center_share": float(np.mean(np.abs(stances) <= 0.2)),
        },
    }
    result["trustworthy"] = contract.topic_is_trust(result)
    log.info("  [%s] ideológica=%.3f afectiva=%.3f compuesta=%.3f %s",
             country, profile.get("ideological", 0),
             profile.get("affective_index", 0), profile.get("composite", 0),
             "✓" if result["trustworthy"] else "⚠ no fiable")
    return result


def _run_grouped(groups: dict, stage: str, mode: str) -> dict:
    """
    Envía juntas las peticiones de varios países o debates (un solo lote
    en modo batch) y devuelve las respuestas repartidas por grupo.
    `groups`: {clave: [(body, meta), ...]} → {clave: [texto|None, ...]}
    """
    flat = [(k, body) for k, items in groups.items() for body, _ in items]
    texts = runner.run([body for _, body in flat], stage, mode)
    out = {k: [] for k in groups}
    for (k, _), t in zip(flat, texts):
        out[k].append(t)
    return out


def analyze_all(headlines: dict[str, list[Article]], countries: list[str],
                min_articles: int, max_debates: int,
                axes_registry: dict[str, list[str]], mode: str,
                stance_passes: int = 1,
                ) -> tuple[dict[str, list[dict]], list[dict]]:
    """
    Tres etapas, cada una para TODOS los países a la vez (en modo batch,
    un lote por etapa en vez de uno por país):
      1. debates del día de cada país
      2. asignación de cada titular a un debate o a ninguno
      3. postura, hostilidad, conflictividad, nosotros/ellos y bloque
         Gobierno ↔ oposición por debate. Con stance_passes > 1, cada
         titular se puntúa varias veces (orden barajado, temperatura 0,5)
         y se promedia; la desviación entre pasadas queda como stance_sd.
    Devuelve (resultados por tema de cada país, un registro por titular).
    """
    results = {c: [] for c in countries}
    records: dict[str, dict] = {}

    # ── 1. Debates ──
    eligible = []
    for c in countries:
        n = len(headlines.get(c, []))
        log.info("--- [%s] %s: %d titulares ---", c,
                 COUNTRY_META.get(c, {}).get("name", c), n)
        if n < min_articles:
            log.info("  [%s] muy pocos titulares → gris", c)
        else:
            eligible.append(c)
    disc = _run_grouped({c: [(build_discover_body(
        [a.title for a in headlines[c]], COUNTRY_META.get(c, {}).get("name", c),
        max_debates), None)] for c in eligible}, "debates", mode)
    debates = {}
    for c in eligible:
        try:
            if disc[c][0] is None:
                raise TransientAPIError("la API no devolvió los debates")
            ds = parse_debates(disc[c][0], max_debates)
        except TransientAPIError as e:
            log.error("  [%s] la API falló al descubrir debates: %s", c, e)
            results[c] = [{"status": "api_error", "stage": "debates",
                           "n_articles": len(headlines[c]), "error": str(e)}]
            continue
        if not ds:
            log.info("  [%s] sin debates identificables → gris", c)
            continue
        # Orientación de polos congelada por etiqueta normalizada.
        debates[c] = [contract.freeze_axis(d, axes_registry) for d in ds]
        for k, d in enumerate(debates[c], 1):
            log.info("  [%s] debate %d: '%s' | %s ↔ %s", c, k,
                     d["label"], d["pole_a"][:45], d["pole_b"][:45])

    # ── 2. Asignación ──
    assign_bodies = {c: build_assign_bodies([a.text() for a in headlines[c]], debates[c])
                     for c in debates}
    assign_texts = _run_grouped(assign_bodies, "asignacion", mode)
    assigned = {}
    for c in debates:
        arts = headlines[c]
        assigned[c] = parse_assign(assign_texts[c], assign_bodies[c], len(debates[c]))
        for a, t in zip(arts, assigned[c]):
            records[a.url] = {
                "country": c, "domain": a.domain, "url": a.url,
                "title": a.title, "lead": a.lead, "title_source": a.title_source,
                "genre": genre_from_url(a.url),
                "topic": (debates[c][t - 1]["label"] if t > 0 else
                          None if t == ASSIGN_NONE else "__failed__"),
            }
        counts = Counter(assigned[c])
        n_failed = counts[ASSIGN_FAILED]
        log.info("  [%s] asignación: %s | ninguno %d | fallidos %d", c,
                 " · ".join(f"{k}:{counts[k]}" for k in range(1, len(debates[c]) + 1)),
                 counts[ASSIGN_NONE], n_failed)
        if n_failed > MAX_FAILED_SHARE * len(arts):
            log.error("  [%s] %d de %d titulares SIN asignar → país descartado",
                      c, n_failed, len(arts))
            results[c] = [{"status": "classification_failed", "stage": "assignment",
                           "n_articles": len(arts), "n_failed": n_failed}]
            del assigned[c]

    # ── 3. Postura y demás dimensiones, por debate ──
    to_score, classify_bodies, perms = {}, {}, {}
    temperature = 0.1 if stance_passes == 1 else 0.5
    for c in assigned:
        for k, d in enumerate(debates[c], 1):
            topic_arts = [a for a, t in zip(headlines[c], assigned[c]) if t == k]
            if len(topic_arts) < min_articles:
                log.info("  [%s] '%s': solo %d titulares asignados → sin métricas",
                         c, d["label"], len(topic_arts))
                results[c].append({
                    "status": "insufficient_after_filtering", "axis": d,
                    "n_articles": len(topic_arts), "n_classified": len(topic_arts),
                    "n_effective": float(len(topic_arts))})
                continue
            to_score[(c, k)] = (d, topic_arts)
            texts = [a.text() for a in topic_arts]
            for p in range(stance_passes):
                # Cada pasada con otro orden: el contexto del lote cambia y
                # la media no depende de qué titulares vinieron juntos.
                order = (list(range(len(texts))) if p == 0 else
                         list(np.random.default_rng(p).permutation(len(texts))))
                perms[(c, k, p)] = order
                classify_bodies[(c, k, p)] = build_classify_bodies(
                    [texts[i] for i in order], d["pole_a"], d["pole_b"],
                    temperature=temperature)
    classify_texts = _run_grouped(classify_bodies, "postura", mode)
    for key, (d, topic_arts) in to_score.items():
        c = key[0]
        log.info("  [%s] '%s': %d titulares", c, d["label"], len(topic_arts))
        passes = []
        for p in range(stance_passes):
            kp = (*key, p)
            res = parse_classify(classify_texts[kp], classify_bodies[kp])
            back = [None] * len(res)
            for pos, i in enumerate(perms[kp]):
                back[i] = res[pos]
            passes.append(back)
        stance_results = combine_passes(passes)
        if stance_passes > 1:
            sds = [r.stance_sd for r in stance_results if np.isfinite(r.stance_sd)]
            if sds:
                log.info("  [%s] consistencia entre %d pasadas: desviación media "
                         "de la postura %.2f", c, stance_passes, float(np.mean(sds)))
        results[c].append(score_topic(c, topic_arts, d, stance_results,
                                      min_articles, records))
    return results, list(records.values())


def build_country_output(country: str, topic_results: list[dict],
                         ) -> dict:
    """
    Construye la salida para UN país: lista de temas con métricas,
    polarización general, y el valor que colorea el mapa.

    ESTRUCTURA DE SALIDA (lo que consume la web):

    {
      "country": "ES",
      "country_name": "España",
      "topics": [
        {
          "rank": 1,
          "label": "Política migratoria",
          "tags": ["IMMIGRATION", "REFUGEES"],
          "axis": {"pole_a": "...", "pole_b": "..."},
          "n_articles": 280,
          "metrics": {
            "ideological": 0.82,     ← tipo 1
            "affective_index": 0.65, ← tipo 2
            "composite": 0.73,
            ...
          },
          "trustworthy": true
        },
        ...
      ],
      "overall": {
        "ideological": 0.71,    ← media de todos los temas
        "affective": 0.58,
        "composite": 0.64,
        "dominant_type": "ideological"  ← cuál pesa más
      },
      "map_color": 0.64,       ← lo que colorea el mapa
      "trustworthy": true       ← si al menos un tema es fiable
    }
    """
    country_name = COUNTRY_META.get(country, {}).get("name", country)

    # Filtrar solo los temas con análisis completado
    ok_topics = [t for t in topic_results if t.get("status") == "ok"]

    if not ok_topics:
        return {
            "country": country,
            "country_name": country_name,
            "topics": [],
            "overall": None,
            "map_color": None,
            "trustworthy": False,
            "status": "no_analyzable_topics",
        }

    # Ordenar temas por polarización compuesta (de mayor a menor)
    ok_topics.sort(
        key=lambda t: t["metrics"].get("composite", 0),
        reverse=True,
    )

    topics_out = []
    for rank, t in enumerate(ok_topics, 1):
        topics_out.append({
            "rank": rank,
            "label": t["axis"]["label"],
            "tags": t.get("tags", []),
            "axis": {
                "pole_a": t["axis"]["pole_a"],
                "pole_b": t["axis"]["pole_b"],
            },
            "n_articles": t["n_classified"],
            "metrics": {
                # ── Tipo 1: Polarización IDEOLÓGICA ──
                # ¿Hay dos bloques? ¿La gente se divide en campamentos?
                "ideological": t["metrics"].get("ideological", 0),
                "bimodality": t["metrics"].get("bimodality", 0),
                "dip_pvalue": t["metrics"].get("dip_pvalue", 1),
                # ── Tipo 2: Polarización AFECTIVA ──
                # ¿Se odian? ¿Hay hostilidad concentrada en los extremos?
                "affective_index": t["metrics"].get("affective_index", 0),
                # ── Compuesta ──
                # Media geométrica de ideológica y afectiva.
                "composite": t["metrics"].get("composite", 0),
                # ── Controles de calidad ──
                "n": t["metrics"].get("n", 0),
                "n_effective": t["metrics"].get("n_effective", 0),
                "sufficient_data": t["metrics"].get("sufficient_data", False),
            },
            "source_quality": t.get("source_quality", {}),
            "trustworthy": t["trustworthy"],
        })

    # Polarización general del país:
    # Promedio de los temas FIABLES, ponderado por nº de artículos.
    # Si no hay temas fiables, usamos todos los que tengan datos.
    pool = [t for t in ok_topics if t["trustworthy"]]
    if not pool:
        pool = ok_topics

    weights = np.array([t["n_classified"] for t in pool], dtype=float)
    if weights.sum() == 0:
        weights = np.ones(len(pool))
    weights = weights / weights.sum()

    avg_ideological = float(np.average(
        [t["metrics"].get("ideological", 0) for t in pool],
        weights=weights,
    ))
    avg_affective = float(np.average(
        [t["metrics"].get("affective_index", 0) for t in pool],
        weights=weights,
    ))
    avg_composite = float(np.average(
        [t["metrics"].get("composite", 0) for t in pool],
        weights=weights,
    ))

    # ¿Qué tipo de polarización domina?
    if avg_ideological > avg_affective * 1.2:
        dominant = "ideological"
    elif avg_affective > avg_ideological * 1.2:
        dominant = "affective"
    else:
        dominant = "mixed"

    any_trustworthy = any(t["trustworthy"] for t in ok_topics)

    return {
        "country": country,
        "country_name": country_name,
        "status": "ok",
        "topics": topics_out,
        "overall": {
            "ideological": round(avg_ideological, 4),
            "affective": round(avg_affective, 4),
            "composite": round(avg_composite, 4),
            "dominant_type": dominant,
        },
        # El valor que colorea el mapa. El compuesto combina las dos
        # dimensiones en un solo número.
        "map_color": round(avg_composite, 4),
        "trustworthy": any_trustworthy,
    }


def report_usage(path: Path, headlines: dict[str, list[Article]],
                 countries: list[str], mode: str) -> dict:
    """
    Consumo REAL de la corrida (usage de cada respuesta de la API), por
    etapa y modelo, con su coste. Lo guarda en `path` y lo resume en el log.
    """
    summary = runner.LEDGER.summary()
    n_items = sum(len(headlines.get(c, [])) for c in countries)
    summary.update({
        "api_mode": mode, "countries": countries, "n_headlines": n_items,
        "usd_per_country": summary["usd_total"] / max(1, len(countries)),
        "usd_per_1000_headlines": summary["usd_total"] / max(1, n_items) * 1000,
        "prices": runner.PRICES, "cache_write": runner.CACHE_WRITE,
        "cache_read": runner.CACHE_READ, "batch_discount": runner.BATCH_DISCOUNT,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log.info("")
    log.info("CONSUMO REAL (%s):", mode)
    for stage, u in summary["by_stage"].items():
        log.info("  %-34s %3d llamadas · entrada %7d · caché escrita %6d · "
                 "caché leída %7d · salida %6d · %.4f $", stage, u["calls"],
                 u["input"], u["cache_write"], u["cache_read"], u["output"], u["usd"])
    log.info("  TOTAL %.4f $ · %.4f $/país · %.4f $ por 1.000 titulares · "
             "%.0f%% del prompt leído de caché", summary["usd_total"],
             summary["usd_per_country"], summary["usd_per_1000_headlines"],
             100 * summary["cache_hit_share"])
    return summary


def load_env(path: Path = ENV_PATH) -> None:
    """
    Carga KEY=valor desde .env (ignorado por git). Gana sobre lo que
    haya exportado en la terminal, para que una key vieja olvidada en
    el entorno no tape la del proyecto.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ[key] = value


def main():
    ap = argparse.ArgumentParser(
        description="Descubrimiento automático de temas polarizantes",
    )
    ap.add_argument(
        "--countries", default=",".join(contract.V1_COUNTRIES),
        help="Códigos de país separados por coma (default: los 10 v1)",
    )
    ap.add_argument(
        "--hours", type=int, default=24,
        help="Ventana temporal en horas (default: 24)",
    )
    ap.add_argument(
        "--every", type=int, default=1,
        help="Muestreo: 1=todos los ficheros (default), 2=uno cada 30 min, "
             "4=uno por hora. GDELT publica cada 15 min y cada fichero "
             "trae noticias nuevas: saltarse ficheros es perder noticias",
    )
    ap.add_argument(
        "--max-debates", type=int, default=MAX_DEBATES,
        help="Máximo de debates a descubrir por país (default: "
             f"{MAX_DEBATES}). El mapa lista los 5 primeros.",
    )
    ap.add_argument(
        "--min-articles", type=int, default=30,
        help="Mínimo de artículos para analizar un tema (default: 30). "
             "Suelo de análisis, no de trust: trust exige n>=80 "
             "(CONTRACT.md §3)",
    )
    ap.add_argument(
        "--stance-passes", type=int, default=1,
        help="Pasadas de puntuación por titular (default 1). Con 3, la "
             "postura es la media de 3 lecturas y se mide la consistencia "
             "del modelo; la etapa de postura cuesta el triple",
    )
    ap.add_argument(
        "--no-leads", action="store_true",
        help="No leer titular real ni entradilla: solo el titular de la URL",
    )
    ap.add_argument(
        "--tdm", choices=("respect", "ignore"), default="respect",
        help="Medios con reserva de minería de textos: 'respect' los deja "
             "fuera (default), 'ignore' los analiza igual",
    )
    ap.add_argument(
        "--api-mode", choices=("batch", "sync"), default="batch",
        help="'batch' (default): Batch API, mitad de precio, puede tardar; "
             "'sync': al momento, precio completo",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Solo reúne y cuenta titulares (sin llamar a la API)",
    )
    ap.add_argument(
        "--out", default=None,
        help="Nombre del archivo de salida en data/ (default: "
             "snapshot.json si se piden los 10 países v1, "
             "snapshot_partial.json si no, para no pisar el mapa)",
    )
    args = ap.parse_args()

    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not args.dry_run:
        # Fallar antes de leer los ficheros, no 20 s después.
        if not key:
            raise SystemExit(
                f"Falta ANTHROPIC_API_KEY. Créala en {ENV_PATH} "
                "(copia .env.example)")
        if "..." in key or len(key) < 60:
            raise SystemExit(
                "ANTHROPIC_API_KEY parece abreviada (la consola muestra "
                "sk-ant-...XXXX). Crea una key nueva y copia la completa.")
        if not key.startswith("sk-ant-") or key.count("sk-ant-") > 1:
            raise SystemExit(
                f"ANTHROPIC_API_KEY mal pegada en {ENV_PATH}: debe empezar "
                "por sk-ant- una sola vez (¿quedó texto de la plantilla?)")

    countries = [c.strip().upper() for c in args.countries.split(",")]
    if args.out is None:
        full_run = set(contract.V1_COUNTRIES) <= set(countries)
        args.out = "snapshot.json" if full_run else "snapshot_partial.json"

    # Fin de ventana: el fichero GKG más reciente cubre 15 min.
    window_end = datetime.strptime(
        gkg_timestamps(args.hours, args.every)[0], "%Y%m%d%H%M%S",
    ) + timedelta(minutes=15)
    ts = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ─────────────────────────────────────────────────────────────
    # PASADA 1: TITULARES
    # ─────────────────────────────────────────────────────────────
    log.info("="*60)
    log.info("PASADA 1: TITULARES")
    log.info("  Países: %s | Ventana: %dh | Muestreo: 1/%d",
             ", ".join(countries), args.hours, args.every)
    log.info("="*60)

    headlines, _ = fetch_country_headlines(
        countries, hours_back=args.hours, every_n=args.every,
        allow_untitled=not args.no_leads,
    )
    for country in countries:
        log.info("[%s] %d titulares analizables", country,
                 len(headlines.get(country, [])))
    if not args.no_leads:
        log.info("Leyendo titular real y entradilla de cada noticia…")
        headlines = enrich_with_leads(headlines,
                                      respect_tdm=args.tdm == "respect")

    if args.dry_run:
        log.info("--- DRY RUN: no se llama a la API de Claude ---")
        return

    # ─────────────────────────────────────────────────────────────
    # PASADA 2: DEBATES, ASIGNACIÓN Y MÉTRICAS POR PAÍS
    # ─────────────────────────────────────────────────────────────
    log.info("")
    log.info("="*60)
    log.info("PASADA 2: ANÁLISIS DE POLARIZACIÓN")
    log.info("="*60)

    axes_registry = (json.loads(AXES_PATH.read_text())
                     if AXES_PATH.exists() else {})
    usage_path = INTERNAL_DIR / f"{Path(args.out).stem}_usage.json"
    runner.LEDGER.reset()
    try:
        all_topic_results, headline_records = analyze_all(
            headlines, countries, min_articles=args.min_articles,
            max_debates=args.max_debates, axes_registry=axes_registry,
            mode=args.api_mode, stance_passes=args.stance_passes,
        )
    finally:
        # El consumo se guarda también si la corrida se corta (p. ej. sin saldo)
        report_usage(usage_path, headlines, countries, args.api_mode)

    # ─────────────────────────────────────────────────────────────
    # CONSTRUIR SALIDA
    # ─────────────────────────────────────────────────────────────
    log.info("")
    log.info("="*60)
    log.info("RESULTADOS")
    log.info("="*60)

    output_countries = {}
    for country in countries:
        topic_results = all_topic_results.get(country, [])
        country_out = build_country_output(country, topic_results)
        output_countries[country] = country_out

        # Imprimir resumen
        name = country_out["country_name"]
        if country_out.get("overall"):
            ov = country_out["overall"]
            log.info("[%s] %s:", country, name)
            log.info("  Ideológica: %.3f | Afectiva: %.3f | "
                     "Compuesta: %.3f | Tipo dominante: %s",
                     ov["ideological"], ov["affective"],
                     ov["composite"], ov["dominant_type"])
            for t in country_out["topics"]:
                m = t["metrics"]
                trust = "✓" if t["trustworthy"] else "⚠"
                log.info("  #%d %-25s  ideo=%.2f  afec=%.2f  "
                         "comp=%.2f  n=%3d  %s",
                         t["rank"], t["label"][:25],
                         m["ideological"], m["affective_index"],
                         m["composite"], m["n"], trust)
        else:
            log.info("[%s] %s: sin datos suficientes", country, name)

    # Detalle interno (dip test, HHI, dominios, notas): fuera del
    # contrato público, se queda en data/internal/.
    INTERNAL_DIR.mkdir(parents=True, exist_ok=True)
    internal = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ts": ts,
        "window_hours": args.hours,
        "every_n": args.every,
        "source": "gdelt_gkg_files",
        "methodology_version": "0.4",
        "raw_topic_results": all_topic_results,
        "countries": output_countries,
    }
    internal_path = INTERNAL_DIR / f"{Path(args.out).stem}_full.json"
    internal_path.write_text(json.dumps(
        internal, indent=2, ensure_ascii=False, default=str))
    AXES_PATH.write_text(json.dumps(
        axes_registry, indent=2, ensure_ascii=False, sort_keys=True))
    # Un titular por línea: qué debate se le asignó y cómo se clasificó.
    headlines_path = INTERNAL_DIR / f"{Path(args.out).stem}_headlines.jsonl"
    headlines_path.write_text("".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in headline_records))

    # Ecosistema de medios: bandos de periódicos, un medio un voto, agenda,
    # opinión y eje Gobierno ↔ oposición (analysis/ecosystem.py). Sin API.
    eco = {c: ecosystem_report(headline_records, country=c) for c in countries
           if any(r.get("country") == c for r in headline_records)}
    eco_path = INTERNAL_DIR / f"{Path(args.out).stem}_ecosystem.json"
    eco_path.write_text(json.dumps(eco, indent=2, ensure_ascii=False, default=str))
    for c, rep in eco.items():
        log.info("")
        log.info("ECOSISTEMA DE MEDIOS [%s]", c)
        for line in summary_lines(rep):
            for part in line.split("\n"):
                log.info("%s", part)

    # Si la API falló en algún tema, el gris del mapa no significaría
    # "pocos datos" sino "no pudimos medir". No se publica.
    failures = [(c, t.get("axis", {}).get("label") or t.get("stage", "?"),
                 t["status"])
                for c, ts_ in all_topic_results.items() for t in ts_
                if t["status"] in ("api_error", "classification_failed")]
    if failures:
        log.error("")
        log.error("%d tema(s) con fallos de la API; NO se publica %s "
                  "(el anterior queda intacto). Detalle en %s",
                  len(failures), OUT_DIR / args.out, internal_path)
        for c, label, status in failures:
            log.error("  [%s] %s: %s", c, label, status)
        raise SystemExit(1)

    # Snapshot público, contrato v1. Escritura atómica: o el fichero
    # nuevo entero, o el anterior intacto.
    snapshot = contract.build_snapshot(all_topic_results, ts, args.hours)
    path = OUT_DIR / args.out
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(contract.dumps(snapshot))
    tmp.replace(path)
    log.info("")
    log.info("Snapshot v1 guardado en %s (detalle en %s)",
             path, internal_path)

    # Tabla resumen compacta
    print()
    print(f"{'país':<18} {'color':>6} {'dom':<5} {'n':>5} {'ne':>5} "
          f"{'temas':>5}  estado")
    print("-" * 60)
    for code, c in snapshot["countries"].items():
        name = COUNTRY_META.get(code, {}).get("name", code)
        n_trust = sum(t["trust"] for t in c["topics"])
        if code not in countries:
            estado = "no pedido"
        elif c["trust"]:
            estado = "fiable"
        else:
            estado = "gris"
        color = f"{c['color']:.3f}" if c["trust"] else "--"
        print(f"{name:<18} {color:>6} {c['dom'] or '--':<5} "
              f"{c['n']:>5} {c['ne']:>5} "
              f"{n_trust:>2}/{len(c['topics']):<2}  {estado}")


if __name__ == "__main__":
    main()
