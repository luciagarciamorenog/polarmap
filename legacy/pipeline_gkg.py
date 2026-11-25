"""
Pipeline basado en ficheros GKG (sin API de GDELT, sin bloqueos).

Uso:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 pipeline_gkg.py --countries ES --topics inmigracion --hours 24

Primera vez: descarga ficheros (~20-40 MB cada uno). Se quedan en data/cache,
así que la segunda ejecución sobre la misma ventana es casi instantánea.

Consejo de uso:
  --hours 24 --every 4   -> 24 ficheros, cobertura de un dia entero (empieza aqui)
  --hours 48 --every 4   -> 48 ficheros, mas volumen
  --hours 24 --every 1   -> 96 ficheros, maxima densidad (pesado)
"""

from __future__ import annotations

import json
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ingest.gkg_files import GKGClient, source_concentration
from analysis.stance import StanceClassifier
from analysis.metrics import polarization_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

OUT_DIR = Path("data")

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
    "IN": {"name": "India", "lang": "eng"},
}


def classify_country(clf, articles, topic, country, min_articles):
    """Clasifica los artículos de un país y calcula su perfil."""
    lang = COUNTRY_META.get(country, {}).get("lang", "eng")
    titles = [a.title for a in articles if a.title.strip()]

    stances, hostilities, confidences = [], [], []
    n_batches = (len(titles) - 1) // clf.batch_size + 1

    for i in range(0, len(titles), clf.batch_size):
        batch = titles[i:i + clf.batch_size]
        results = clf.classify_batch(batch, topic, language=lang)
        for r in results:
            if r.off_topic or not np.isfinite(r.stance):
                continue
            stances.append(r.stance)
            hostilities.append(r.hostility)
            confidences.append(r.confidence)
        log.info("  [%s] lote %d/%d", country, i // clf.batch_size + 1, n_batches)
        time.sleep(0.4)

    if len(stances) < min_articles:
        return None, len(stances)

    profile = polarization_profile(
        np.array(stances), np.array(hostilities),
        weights=np.array(confidences, dtype=float),
    )
    return profile, len(stances)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", default="ES")
    ap.add_argument("--topics", default="inmigracion")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--every", type=int, default=4,
                    help="1=todos los ficheros, 4=uno por hora")
    ap.add_argument("--min-articles", type=int, default=60)
    ap.add_argument("--out", default="snapshot.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="descarga y filtra, pero NO llama a la API de Claude")
    args = ap.parse_args()

    countries = [c.strip().upper() for c in args.countries.split(",")]
    topics = [t.strip() for t in args.topics.split(",")]

    gk = GKGClient()
    clf = None if args.dry_run else StanceClassifier()

    results = []
    for topic in topics:
        log.info("=== %s: descargando ficheros GKG ===", topic)
        articles, totals = gk.fetch(countries, topic,
                                    hours_back=args.hours, every_n=args.every)
        log.info("ficheros ok=%d faltan=%d | filas=%d | tema=%d pais=%d "
                 "sin_titular=%d utiles=%d",
                 totals["files_ok"], totals["files_missing"], totals["rows"],
                 totals["topic_match"], totals["country_match"],
                 totals["no_title"], totals["kept"])

        for country in countries:
            arts = [a for a in articles if a.country == country]
            conc = source_concentration(arts)
            log.info("[%s/%s] %d articulos, %d dominios, HHI=%.3f",
                     country, topic, len(arts), conc["n_domains"], conc["hhi"])

            if len(arts) < args.min_articles:
                results.append({
                    "country": country, "topic": topic, "n": len(arts),
                    "status": "insufficient_data",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                continue

            if args.dry_run:
                log.info("[%s/%s] DRY RUN: %d articulos listos, no se clasifica",
                         country, topic, len(arts))
                for a in arts[:5]:
                    log.info("    ejemplo: [%s] %s", a.domain, a.title[:70])
                results.append({
                    "country": country, "topic": topic, "n": len(arts),
                    "status": "dry_run",
                    "source_quality": {k: conc[k] for k in
                                       ("n_domains", "hhi", "top_share", "reliable")},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                continue

            profile, n_ok = classify_country(clf, arts, topic, country,
                                             args.min_articles)
            if profile is None:
                results.append({
                    "country": country, "topic": topic, "n": n_ok,
                    "status": "insufficient_after_filtering",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                continue

            results.append({
                "country": country,
                "country_name": COUNTRY_META.get(country, {}).get("name", country),
                "topic": topic,
                "status": "ok",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "window_hours": args.hours,
                "metrics": profile,
                "source_quality": {k: conc[k] for k in
                                   ("n_domains", "hhi", "top_share", "reliable")},
                "trustworthy": bool(
                    profile.get("sufficient_data") and conc["reliable"]
                    and profile.get("n_effective", 0) >= 50
                ),
            })

    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / args.out
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": args.hours,
        "source": "gdelt_gkg_files",
        "methodology_version": "0.2",
        "results": results,
    }, indent=2, ensure_ascii=False))

    log.info("escrito %s", path)

    print("\n{:<16} {:<14} {:>7} {:>7} {:>7} {:>6}  estado".format(
        "pais", "tema", "ideol", "afect", "comp", "n"))
    print("-" * 70)
    for r in results:
        if r.get("status") != "ok":
            print(f"{r['country']:<16} {r['topic']:<14} {'--':>7} {'--':>7} "
                  f"{'--':>7} {r.get('n', 0):>6}  {r['status']}")
            continue
        m = r["metrics"]
        print(f"{r['country_name']:<16} {r['topic']:<14} "
              f"{m['ideological']:>7.3f} {m['affective_index']:>7.3f} "
              f"{m['composite']:>7.3f} {m['n']:>6}  "
              f"{'fiable' if r['trustworthy'] else 'NO fiable'}")


if __name__ == "__main__":
    main()
