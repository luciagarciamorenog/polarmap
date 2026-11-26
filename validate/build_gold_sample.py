"""
Muestra para el set de referencia (Nivel 3).

Toma el registro por titular de una corrida (data/internal/*_headlines.jsonl)
y saca una muestra para etiquetar a mano, a ciegas: la página de etiquetado
solo recibe titular, entradilla, medio y URL; las etiquetas del modelo y el
estrato de cada titular se quedan en el fichero de muestra para puntuar
después.

Dos diseños:

  classic (sept 2026): la mitad, titulares asignados a un debate (repartidos
      entre debates); la otra mitad, titulares que el modelo mandó a
      "ninguno". Uso:
          python3 validate/build_gold_sample.py --run snapshot_partial

  tandas (nov 2026): más peso en los casos difíciles, en tandas iguales para
      que cada tanda de 50 sea ya una mezcla completa. Cuatro estratos, según
      lo que respondió el modelo:
        H  asignado a un debate y hostilidad >= 0,5
        S  asignado, hostilidad < 0,5 y |postura| >= 0,5   (postura clara)
        R  resto de asignados
        N  "ninguno"
      Por defecto 5 tandas de 50 = 250 (14 H + 12 S + 14 R + 10 N por tanda).
      Se guarda el tamaño de cada estrato en el día para poder reponderar
      (p. ej. al estimar el % real de titulares hostiles). Solo titulares con
      titular real de la página (los reconstruidos de la URL son ~3 % y no se
      pueden leer bien). Uso:
          python3 validate/build_gold_sample.py --design tandas \\
              --run snapshot_ES_20261126
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

# Por tanda: cuántos de cada estrato
PER_BATCH = {"H": 14, "S": 12, "R": 14, "N": 10}


def stratified_assigned(rows: list[dict], k: int, rng: random.Random,
                        min_per_debate: int = 12) -> list[dict]:
    """k titulares asignados, proporcional al tamaño de cada debate pero
    con un mínimo por debate para poder medir los pequeños."""
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r)
    total = sum(len(v) for v in by_topic.values())
    quota = {t: min(len(v), max(min_per_debate, round(k * len(v) / total)))
             for t, v in by_topic.items()}
    # Ajustar al total pedido quitando del debate más grande
    while sum(quota.values()) > k:
        biggest = max(quota, key=quota.get)
        quota[biggest] -= 1
    # Si faltan (debates pequeños sin bastantes), rellenar de los grandes
    while sum(quota.values()) < k:
        room = [t for t, v in by_topic.items() if quota[t] < len(v)]
        if not room:
            break
        quota[max(room, key=lambda t: len(by_topic[t]) - quota[t])] += 1
    picked = []
    for t, v in by_topic.items():
        picked += rng.sample(v, quota[t])
    return picked


def stratum(r: dict) -> str:
    if r["topic"] is None:
        return "N"
    if (r.get("hostility") or 0) >= 0.5:
        return "H"
    if abs(r.get("stance") or 0) >= 0.5:
        return "S"
    return "R"


def design_classic(rows, per_group, rng):
    assigned = [r for r in rows if r["topic"] not in (None, "__failed__")]
    none = [r for r in rows if r["topic"] is None]
    sample = (stratified_assigned(assigned, per_group, rng)
              + rng.sample(none, min(per_group, len(none))))
    rng.shuffle(sample)
    return sample, None


def design_tandas(rows, n_batches, rng):
    rows = [r for r in rows if r["topic"] != "__failed__"
            and r.get("title_source") == "page"]
    by_s: dict[str, list[dict]] = {"H": [], "S": [], "R": [], "N": []}
    for r in rows:
        by_s[stratum(r)].append(r)
    picked: dict[str, list[dict]] = {}
    for s, per in PER_BATCH.items():
        k = min(per * n_batches, len(by_s[s]))
        if s == "N":
            picked[s] = rng.sample(by_s[s], k)
        else:
            picked[s] = stratified_assigned(by_s[s], k, rng, min_per_debate=4)
        rng.shuffle(picked[s])
    sample, batches = [], []
    for b in range(n_batches):
        batch = []
        for s, per in PER_BATCH.items():
            chunk = picked[s][b * per:(b + 1) * per]
            for r in chunk:
                r["_stratum"] = s
            batch += chunk
        rng.shuffle(batch)
        sample += batch
        batches.append(len(batch))
    strata = {"definition": {
                  "H": "asignado y hostilidad >= 0.5",
                  "S": "asignado, hostilidad < 0.5 y |postura| >= 0.5",
                  "R": "resto de asignados",
                  "N": "ninguno"},
              "population": {s: len(v) for s, v in by_s.items()},
              "sampled": dict(Counter(r["_stratum"] for r in sample)),
              "batches": batches,
              "excluded_url_titles": True}
    return sample, strata


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="snapshot_partial",
                    help="prefijo de la corrida en data/internal/")
    ap.add_argument("--country", default="ES")
    ap.add_argument("--design", choices=["classic", "tandas"],
                    default="classic")
    ap.add_argument("--per-group", type=int, default=75,
                    help="solo classic")
    ap.add_argument("--batches", type=int, default=5,
                    help="solo tandas: número de tandas de 50")
    ap.add_argument("--seed", type=int, default=20260925)
    args = ap.parse_args()

    rows = [json.loads(line) for line in
            (INTERNAL / f"{args.run}_headlines.jsonl").read_text().splitlines()]
    rows = [r for r in rows if r["country"] == args.country]
    full = json.loads((INTERNAL / f"{args.run}_full.json").read_text())
    debates = [t["axis"] for t in full["raw_topic_results"][args.country]
               if t.get("axis")]
    rng = random.Random(args.seed)

    if args.design == "classic":
        sample, strata = design_classic(rows, args.per_group, rng)
    else:
        sample, strata = design_tandas(rows, args.batches, rng)

    items = []
    for i, r in enumerate(sample, 1):
        it = {
            "id": f"{args.country}-{i:03d}",
            "order": i,
            "title": r["title"],
            "lead": r.get("lead") or "",
            "domain": r["domain"],
            "url": r["url"],
            "model": {k: r.get(k) for k in
                      ("topic", "stance", "hostility", "confidence",
                       "off_topic", "us_them", "conflict", "target",
                       "speaker", "bloc", "why", "stance_sd")},
        }
        if "_stratum" in r:
            it["stratum"] = r["_stratum"]
        items.append(it)

    assigned = [r for r in rows if r["topic"] not in (None, "__failed__")]
    GOLD.mkdir(parents=True, exist_ok=True)
    out = GOLD / f"sample_{args.country}_{full['ts'][:10].replace('-', '')}.json"
    out.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "design": args.design,
        "source_run": {"run": args.run, "ts": full["ts"],
                       "every_n": full.get("every_n"),
                       "methodology_version": full.get("methodology_version")},
        "country": args.country,
        "seed": args.seed,
        # Para extrapolar proporciones de la muestra al total del día
        "population": {"total": len(rows), "assigned": len(assigned),
                       "none": sum(1 for r in rows if r["topic"] is None),
                       "by_topic": dict(Counter(r["topic"] for r in assigned))},
        "strata": strata,
        "debates": [{"label": d["label"], "pole_a": d["pole_a"],
                     "pole_b": d["pole_b"]} for d in debates],
        "items": items,
    }, ensure_ascii=False, indent=1))
    print(f"{len(items)} titulares → {out}")
    for t, n in Counter(it["model"]["topic"] for it in items).most_common():
        print(f"  {n:3d}  {t or 'ninguno'}")
    if strata:
        print("estratos en el día:", strata["population"])
        print("en la muestra:     ", strata["sampled"])


if __name__ == "__main__":
    main()
