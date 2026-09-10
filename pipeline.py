"""
Pipeline completo: GDELT -> stance -> métricas -> JSON para el mapa.

Uso:
    export ANTHROPIC_API_KEY=sk-...
    python pipeline.py --countries ES,US,FR --topics inmigracion --hours 168
"""

from __future__ import annotations

import os
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ingest.gdelt import GDELTClient, COUNTRIES, TOPICS, source_concentration
from analysis.stance import StanceClassifier
from analysis.metrics import polarization_profile

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

OUT_DIR = Path("data")


def run_country_topic(gd: GDELTClient, clf: StanceClassifier,
                      country: str, topic: str, hours: int,
                      min_articles: int = 60) -> dict | None:
    log.info("[%s/%s] descargando...", country, topic)
    articles = gd.fetch(country, topic, hours_back=hours, max_records=250)

    if len(articles) < min_articles:
        log.warning("[%s/%s] solo %d articulos (<%d), se marca insuficiente",
                    country, topic, len(articles), min_articles)
        return {
            "country": country, "topic": topic, "n": len(articles),
            "status": "insufficient_data",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    conc = source_concentration(articles)
    log.info("[%s/%s] %d articulos, %d dominios, HHI=%.3f",
             country, topic, conc["n"], conc["n_domains"], conc["hhi"])

    # Clasificar por lotes
    titles = [a.title for a in articles if a.title.strip()]
    lang = COUNTRIES[country]["lang"]
    stances, hostilities, confidences = [], [], []

    for i in range(0, len(titles), clf.batch_size):
        batch = titles[i:i + clf.batch_size]
        results = clf.classify_batch(batch, topic, language=lang)
        for r in results:
            if r.off_topic or not np.isfinite(r.stance):
                continue
            stances.append(r.stance)
            hostilities.append(r.hostility)
            confidences.append(r.confidence)
        log.info("  lote %d/%d", i // clf.batch_size + 1,
                 (len(titles) - 1) // clf.batch_size + 1)
        time.sleep(0.5)

    if len(stances) < min_articles:
        return {"country": country, "topic": topic, "n": len(stances),
                "status": "insufficient_after_filtering",
                "timestamp": datetime.now(timezone.utc).isoformat()}

    # Ponderar por confianza del clasificador: un titular ambiguo pesa menos
    weights = np.array(confidences, dtype=float)
    profile = polarization_profile(
        np.array(stances), np.array(hostilities), weights=weights
    )

    return {
        "country": country,
        "country_name": COUNTRIES[country]["name"],
        "topic": topic,
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_hours": hours,
        "metrics": profile,
        "source_quality": {
            "n_domains": conc["n_domains"],
            "hhi": conc["hhi"],
            "top_share": conc["top_share"],
            "reliable": conc["reliable"],
        },
        # Bandera de fiabilidad combinada. El mapa debe pintar en gris,
        # no en color, todo lo que no la cumpla. Mostrar un número poco
        # fiable con color fuerte es peor que no mostrar nada.
        "trustworthy": bool(
            profile.get("sufficient_data") and conc["reliable"]
            and profile.get("n_effective", 0) >= 50
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", default="ES,US,FR,DE,IT",
                    help="códigos separados por coma, o 'all'")
    ap.add_argument("--topics", default="inmigracion")
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--out", default="snapshot.json")
    args = ap.parse_args()

    countries = list(COUNTRIES) if args.countries == "all" \
        else [c.strip().upper() for c in args.countries.split(",")]
    topics = [t.strip() for t in args.topics.split(",")]

    gd = GDELTClient()
    clf = StanceClassifier()

    results = []
    for country in countries:
        for topic in topics:
            try:
                r = run_country_topic(gd, clf, country, topic, args.hours)
                if r:
                    results.append(r)
            except Exception as e:
                log.exception("fallo en %s/%s: %s", country, topic, e)

    OUT_DIR.mkdir(exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": args.hours,
        "methodology_version": "0.1",
        "results": results,
    }
    path = OUT_DIR / args.out
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log.info("escrito %s (%d registros, %d fiables)", path, len(results),
             sum(1 for r in results if r.get("trustworthy")))

    print("\n{:<16} {:<14} {:>7} {:>7} {:>7} {:>6}  fiable".format(
        "pais", "tema", "ideol", "afect", "comp", "n"))
    print("-" * 68)
    for r in results:
        if r.get("status") != "ok":
            print(f"{r['country']:<16} {r['topic']:<14} {'--':>7} {'--':>7} "
                  f"{'--':>7} {r.get('n', 0):>6}  {r['status']}")
            continue
        m = r["metrics"]
        print(f"{r['country_name']:<16} {r['topic']:<14} "
              f"{m['ideological']:>7.3f} {m['affective_index']:>7.3f} "
              f"{m['composite']:>7.3f} {m['n']:>6}  {r['trustworthy']}")


if __name__ == "__main__":
    main()
