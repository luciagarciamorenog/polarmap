"""
Pipeline con descubrimiento automático de temas.

EN VEZ DE DECIRLE AL SISTEMA QUÉ TEMAS BUSCAR, ESTE PIPELINE:
  1. Baja los ficheros GKG (con caché, como antes)
  2. Cuenta de qué temas hablan los medios de cada país (CENSO)
  3. Agrupa etiquetas relacionadas (IMMIGRATION+REFUGEES = un tema)
  4. Para los temas más frecuentes:
     a. Extrae titulares (de los ficheros cacheados — instantáneo)
     b. Claude define el eje de debate
     c. Clasifica posturas
     d. Calcula métricas de polarización (ideológica y afectiva)
  5. Genera snapshot.json con el contrato v1 (CONTRACT.md) para el mapa,
     y el detalle interno en data/internal/

Uso (la key se lee de .env en la raíz del proyecto; ver .env.example):
    python3 pipeline_discover.py                 # los 10 países v1
    python3 pipeline_discover.py --countries ES --hours 24

    # Solo el censo (sin gastar API):
    python3 pipeline_discover.py --countries ES --hours 24 --census-only

    # Más países:
    python3 pipeline_discover.py --countries ES,FR,DE,GB --hours 24

Coste típico (5 temas, 1 país):
    ~6 llamadas a Claude Sonnet → ~$0.10-0.15
"""

from __future__ import annotations

import os
import json
import argparse
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from analysis import contract
from analysis.discover import (
    run_census, build_topic_groups, fetch_articles_for_group,
    discover_axis, classify_stances, source_concentration,
    TopicGroup, TransientAPIError,
)
from analysis.metrics import polarization_profile
from ingest.gkg_files import gkg_timestamps

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


def analyze_topic(group: TopicGroup, countries: list[str],
                  hours_back: int, every_n: int, max_files: int,
                  min_articles: int, api_key: str | None,
                  axes_registry: dict[str, list[str]],
                  ) -> dict[str, dict]:
    """
    Analiza UN grupo temático para todos los países pedidos.

    1. Extrae artículos del grupo (pasada 2, desde caché)
    2. Para cada país con suficientes artículos:
       a. Claude descubre el eje (si no lo tiene ya)
       b. Clasifica posturas
       c. Calcula métricas

    Devuelve: {country: resultado_dict}
    """
    log.info("--- Analizando grupo: %s (%d artículos aprox) ---",
             ", ".join(group.tags[:3]), group.count)

    # Extraer artículos de los ficheros cacheados
    articles_by_country = fetch_articles_for_group(
        group.tags, countries,
        hours_back=hours_back, every_n=every_n, max_files=max_files,
    )

    results = {}

    for country in countries:
        arts = articles_by_country.get(country, [])
        country_name = COUNTRY_META.get(country, {}).get("name", country)
        lang = COUNTRY_META.get(country, {}).get("lang", "eng")

        if len(arts) < min_articles:
            log.info("  [%s] solo %d artículos, mínimo es %d → saltar",
                     country, len(arts), min_articles)
            results[country] = {
                "status": "insufficient_data",
                "n_articles": len(arts),
                "min_required": min_articles,
            }
            continue

        # Concentración cruda, solo para el log: la que decide trust se
        # mide abajo sobre los titulares on-topic (CONTRACT.md §3).
        conc_raw = source_concentration(arts)
        log.info("  [%s] %d artículos, %d medios, HHI=%.3f (crudo)",
                 country, len(arts), conc_raw["n_domains"],
                 conc_raw["hhi"])

        # Descubrir el eje de debate (una llamada a Claude)
        arts = [a for a in arts if a.title.strip()]
        titles = [a.title for a in arts]
        try:
            axis = discover_axis(
                titles, group.tags, country_name, api_key=api_key,
            )
        except TransientAPIError as e:
            log.error("  [%s] la API falló al descubrir el eje: %s",
                      country, e)
            results[country] = {
                "status": "api_error",
                "n_articles": len(arts),
                "error": str(e),
            }
            continue
        if axis is None:
            log.info("  [%s] sin debate claro → saltar", country)
            results[country] = {
                "status": "no_clear_debate",
                "n_articles": len(arts),
            }
            continue

        # Si el tema ya salió antes, reutilizar sus polos (orientación
        # congelada por etiqueta normalizada).
        axis = contract.freeze_axis(axis, axes_registry)

        log.info("  [%s] eje: '%s' | %s ↔ %s",
                 country, axis["label"], axis["pole_a"][:40],
                 axis["pole_b"][:40])

        # Guardar eje en el grupo (para el JSON de salida)
        group.label = axis["label"]
        group.pole_a = axis["pole_a"]
        group.pole_b = axis["pole_b"]

        # Clasificar posturas de todos los titulares
        stance_results = classify_stances(
            titles, axis["pole_a"], axis["pole_b"],
            language=lang, api_key=api_key,
        )

        # Filtrar los que son off-topic o sin confianza
        # classify_stances devuelve un resultado por titular, en orden.
        n_failed = sum(r.failed for r in stance_results)
        if n_failed > MAX_FAILED_SHARE * len(stance_results):
            log.error("  [%s] %d de %d titulares SIN clasificar (API o "
                      "respuesta ilegible) → tema descartado",
                      country, n_failed, len(stance_results))
            results[country] = {
                "status": "classification_failed",
                "axis": axis,
                "n_articles": len(arts),
                "n_failed": n_failed,
            }
            continue

        stances, hostilities, confidences, on_topic = [], [], [], []
        for art, r in zip(arts, stance_results):
            if r.failed or r.off_topic or not np.isfinite(r.stance):
                continue
            on_topic.append(art)
            stances.append(r.stance)
            hostilities.append(r.hostility)
            confidences.append(r.confidence)

        # DIAGNÓSTICO
        n_off = len(stance_results) - len(stances) - n_failed
        log.info('  [%s] clasificación: %d on-topic, %d off-topic, '
                 '%d fallidos de %d total', country, len(stances), n_off,
                 n_failed, len(stance_results))
        if stances:
            s = np.array(stances)
            h = np.array(hostilities)
            c = np.array(confidences)
            log.info('  [%s] stances:  min=%.2f max=%.2f mean=%.2f std=%.2f',
                     country, s.min(), s.max(), s.mean(), s.std())
            log.info('  [%s] hostil:   min=%.2f max=%.2f mean=%.2f',
                     country, h.min(), h.max(), h.mean())
            log.info('  [%s] confid:   min=%.2f max=%.2f mean=%.2f',
                     country, c.min(), c.max(), c.mean())

        if len(stances) < min_articles:
            log.info("  [%s] solo %d clasificaciones útiles → no fiable",
                     country, len(stances))
            results[country] = {
                "status": "insufficient_after_filtering",
                "axis": axis,
                "n_articles": len(arts),
                "n_classified": len(stances),
                "n_effective": float(len(stances)),
                "n_failed": n_failed,
            }
            continue

        # Calcular métricas de polarización (ideológica + afectiva)
        # min_n bajo porque ya filtramos por min_articles y calidad
        profile = polarization_profile(
            np.array(stances),
            np.array(hostilities),
            weights=np.array(confidences, dtype=float),
            min_n=min_articles,
        )

        conc = source_concentration(on_topic)
        results[country] = {
            "status": "ok",
            "axis": axis,
            "n_articles": len(arts),
            "n_classified": len(stances),
            "n_effective": profile.get("n_effective", 0),
            "n_failed": n_failed,
            "metrics": profile,
            "source_quality": {
                k: conc[k] for k in
                ("n_domains", "hhi", "top_share", "reliable")
            },
        }
        trustworthy = contract.topic_is_trust(results[country])
        results[country]["trustworthy"] = trustworthy

        log.info("  [%s] ideológica=%.3f afectiva=%.3f compuesta=%.3f %s",
                 country,
                 profile.get("ideological", 0),
                 profile.get("affective_index", 0),
                 profile.get("composite", 0),
                 "✓" if trustworthy else "⚠ no fiable")

    return results


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
        "--every", type=int, default=4,
        help="Muestreo: 1=todos los ficheros, 4=uno por hora (default: 4)",
    )
    ap.add_argument(
        "--top-n", type=int, default=5,
        help="Cuántos temas analizar por país (default: 5)",
    )
    ap.add_argument(
        "--min-articles", type=int, default=30,
        help="Mínimo de artículos para analizar un tema (default: 30). "
             "Suelo de análisis, no de trust: trust exige n>=80 "
             "(CONTRACT.md §3)",
    )
    ap.add_argument(
        "--census-only", action="store_true",
        help="Solo ejecuta el censo (sin llamar a la API de Claude)",
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
    if not args.census_only:
        # Fallar antes del censo, no 20 s después.
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
    # PASADA 1: CENSO
    # ─────────────────────────────────────────────────────────────
    log.info("="*60)
    log.info("PASADA 1: CENSO DE TEMAS")
    log.info("  Países: %s | Ventana: %dh | Muestreo: 1/%d",
             ", ".join(countries), args.hours, args.every)
    log.info("="*60)

    census = run_census(
        countries,
        hours_back=args.hours,
        every_n=args.every,
    )

    log.info("Censo terminado: %d filas leídas, %d de países pedidos",
             census.total_rows, census.total_country)

    # Mostrar resumen del censo por país
    for country in countries:
        counts = census.counts.get(country)
        if not counts:
            log.info("[%s] sin datos en el censo", country)
            continue

        log.info("[%s] %d etiquetas temáticas distintas",
                 country, len(counts))

        # Construir grupos temáticos
        cooccur = census.cooccur.get(country, {})
        groups = build_topic_groups(
            counts, cooccur,
            min_articles=args.min_articles,
        )
        log.info("[%s] %d grupos temáticos (tras fusionar y filtrar)",
                 country, len(groups))

        # Mostrar los principales
        for i, g in enumerate(groups[:args.top_n + 3]):
            tags_str = ", ".join(g.tags[:4])
            extra = f" +{len(g.tags)-4}" if len(g.tags) > 4 else ""
            marker = " ←" if i < args.top_n else ""
            log.info("  #%d  %4d arts  [%s%s]%s",
                     i + 1, g.count, tags_str, extra, marker)

    if args.census_only:
        log.info("--- MODO CENSO: no se llama a la API de Claude ---")
        # Guardar el censo como JSON para inspección
        census_out = {}
        for country in countries:
            counts = census.counts.get(country, {})
            cooccur = census.cooccur.get(country, {})
            groups = build_topic_groups(counts, cooccur,
                                        min_articles=args.min_articles)
            census_out[country] = {
                "country_name": COUNTRY_META.get(country, {}).get(
                    "name", country),
                "n_themes": len(counts),
                "n_groups": len(groups),
                "top_groups": [
                    {"tags": g.tags, "count": g.count}
                    for g in groups[:args.top_n + 3]
                ],
            }

        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / "census.json"
        path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": args.hours,
            "total_rows": census.total_rows,
            "countries": census_out,
        }, indent=2, ensure_ascii=False))
        log.info("Censo guardado en %s", path)
        return

    # ─────────────────────────────────────────────────────────────
    # PASADA 2: ANÁLISIS POR TEMA
    # ─────────────────────────────────────────────────────────────
    log.info("")
    log.info("="*60)
    log.info("PASADA 2: ANÁLISIS DE POLARIZACIÓN")
    log.info("="*60)

    # Para cada país, seleccionar los top N grupos
    country_groups: dict[str, list[TopicGroup]] = {}
    for country in countries:
        counts = census.counts.get(country, {})
        cooccur = census.cooccur.get(country, {})
        groups = build_topic_groups(counts, cooccur,
                                    min_articles=args.min_articles)
        country_groups[country] = groups[:args.top_n]

    # Reunir TODOS los grupos únicos (algunos países pueden compartir
    # temas, así que deduplicamos por conjunto de etiquetas)
    unique_groups: dict[str, TopicGroup] = {}
    group_to_countries: dict[str, list[str]] = defaultdict(list)
    for country, groups in country_groups.items():
        for g in groups:
            key = "|".join(sorted(g.tags))
            if key not in unique_groups:
                unique_groups[key] = g
            group_to_countries[key].append(country)

    log.info("%d grupos temáticos únicos a analizar", len(unique_groups))

    # Analizar cada grupo para los países que lo tienen
    all_topic_results: dict[str, list[dict]] = defaultdict(list)
    axes_registry = (json.loads(AXES_PATH.read_text())
                     if AXES_PATH.exists() else {})

    for key, group in unique_groups.items():
        group_countries = group_to_countries[key]
        results = analyze_topic(
            group, group_countries,
            hours_back=args.hours,
            every_n=args.every,
            max_files=60,
            min_articles=args.min_articles,
            api_key=None,  # usa ANTHROPIC_API_KEY del entorno
            axes_registry=axes_registry,
        )

        for country, result in results.items():
            # Adjuntar los tags del grupo al resultado
            result["tags"] = group.tags
            all_topic_results[country].append(result)

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
        "methodology_version": "0.3",
        "raw_topic_results": all_topic_results,
        "countries": output_countries,
    }
    internal_path = INTERNAL_DIR / f"{Path(args.out).stem}_full.json"
    internal_path.write_text(json.dumps(
        internal, indent=2, ensure_ascii=False, default=str))
    AXES_PATH.write_text(json.dumps(
        axes_registry, indent=2, ensure_ascii=False, sort_keys=True))

    # Si la API falló en algún tema, el gris del mapa no significaría
    # "pocos datos" sino "no pudimos medir". No se publica.
    failures = [(c, t.get("axis", {}).get("label") or "|".join(t["tags"]),
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
