"""
Muestra para el set de referencia (Nivel 3).

Toma el registro por titular de una corrida (data/internal/*_headlines.jsonl)
y saca una muestra estratificada:
  - la mitad, titulares que el modelo asignó a un debate (repartidos entre
    debates), para medir asignación, postura y hostilidad;
  - la otra mitad, titulares que el modelo mandó a "ninguno", para medir
    qué contiene ese bloque (sin postura, política sin debate, controversia
    no política...).

El orden se baraja para que quien etiqueta no vea bloques por debate, y la
página de etiquetado solo recibe titular, medio y URL: las etiquetas del
modelo se quedan en este fichero para puntuar después (etiquetado a ciegas).

Uso:
    python3 validate/build_gold_sample.py --country ES --per-group 75
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

INTERNAL = Path("data/internal")
GOLD = Path("data/gold")


def stratified_assigned(rows: list[dict], k: int, rng: random.Random,
                        min_per_debate: int = 12) -> list[dict]:
    """k titulares asignados, proporcional al tamaño de cada debate pero
    con un mínimo por debate para poder medir los pequeños."""
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r)
    total = sum(len(v) for v in by_topic.values())
    quota = {t: max(min_per_debate, round(k * len(v) / total))
             for t, v in by_topic.items()}
    # Ajustar al total pedido quitando del debate más grande
    while sum(quota.values()) > k:
        biggest = max(quota, key=quota.get)
        quota[biggest] -= 1
    picked = []
    for t, v in by_topic.items():
        picked += rng.sample(v, min(quota[t], len(v)))
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="snapshot_partial",
                    help="prefijo de la corrida en data/internal/")
    ap.add_argument("--country", default="ES")
    ap.add_argument("--per-group", type=int, default=75)
    ap.add_argument("--seed", type=int, default=20260925)
    args = ap.parse_args()

    rows = [json.loads(line) for line in
            (INTERNAL / f"{args.run}_headlines.jsonl").read_text().splitlines()]
    rows = [r for r in rows if r["country"] == args.country]
    full = json.loads((INTERNAL / f"{args.run}_full.json").read_text())
    debates = [t["axis"] for t in full["raw_topic_results"][args.country]
               if t.get("axis")]

    assigned = [r for r in rows if r["topic"] not in (None, "__failed__")]
    none = [r for r in rows if r["topic"] is None]
    rng = random.Random(args.seed)
    sample = (stratified_assigned(assigned, args.per_group, rng)
              + rng.sample(none, min(args.per_group, len(none))))
    rng.shuffle(sample)

    items = []
    for i, r in enumerate(sample, 1):
        items.append({
            "id": f"{args.country}-{i:03d}",
            "title": r["title"],
            "domain": r["domain"],
            "url": r["url"],
            "model": {k: r.get(k) for k in
                      ("topic", "stance", "hostility", "confidence",
                       "off_topic")},
        })

    GOLD.mkdir(parents=True, exist_ok=True)
    out = GOLD / f"sample_{args.country}_{full['ts'][:10].replace('-', '')}.json"
    out.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_run": {"ts": full["ts"], "every_n": full.get("every_n"),
                       "methodology_version": full.get("methodology_version")},
        "country": args.country,
        "seed": args.seed,
        # Para extrapolar proporciones de la muestra al total del día
        "population": {"total": len(rows), "assigned": len(assigned),
                       "none": len(none),
                       "by_topic": dict(Counter(r["topic"] for r in assigned))},
        "debates": [{"label": d["label"], "pole_a": d["pole_a"],
                     "pole_b": d["pole_b"]} for d in debates],
        "items": items,
    }, ensure_ascii=False, indent=1))
    c = Counter(it["model"]["topic"] for it in items)
    print(f"{len(items)} titulares → {out}")
    for t, n in c.most_common():
        print(f"  {n:3d}  {t or 'ninguno'}")


if __name__ == "__main__":
    main()
