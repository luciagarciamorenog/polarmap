"""
Reclasifica una muestra gold con el pipeline actual (asignación + postura
v2 sobre titular + entradilla) y deja un fichero de muestra con esas
etiquetas como "model", listo para puntuarlo con score_gold.py.

Usa los debates fijos de la muestra para que la comparación con el set de
referencia sea justa. Respeta la reserva TDM como el pipeline (--tdm).

Uso:
    python3 validate/eval_classifier.py data/gold/sample_ES_20260925.json
    python3 validate/score_gold.py data/gold/sample_ES_20260925_v2model.json \\
        data/gold/silver_ES_20260925_v2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.discover import (  # noqa: E402
    ASSIGN_FAILED, ASSIGN_NONE, assign_topics, classify_stances,
)
from pipeline_discover import load_env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sample")
    ap.add_argument("--tdm", choices=("respect", "ignore"), default="respect")
    args = ap.parse_args()
    load_env()

    sample_path = Path(args.sample)
    sample = json.loads(sample_path.read_text())
    leads = json.loads(sample_path.with_name(
        sample_path.name.replace("sample_", "leads_")).read_text())
    debates = sample["debates"]

    items, skipped = [], 0
    for it in sample["items"]:
        ld = leads.get(it["id"], {})
        if args.tdm == "respect" and ld.get("tdm_reserved"):
            skipped += 1
            continue
        title = ld.get("title") or it["title"]
        lead = ld.get("description", "")
        items.append((it, f"{title}. {lead}" if lead else title))
    print(f"{len(items)} titulares ({skipped} fuera por reserva TDM)")

    assigned = assign_topics([t for _, t in items], debates)
    new_items = []
    for k, d in enumerate(debates, 1):
        group = [(it, t) for (it, t), a in zip(items, assigned) if a == k]
        if not group:
            continue
        results = classify_stances([t for _, t in group], d["pole_a"], d["pole_b"])
        for (it, _), r in zip(group, results):
            new_items.append(dict(it, model={
                "topic": d["label"],
                "stance": None if r.failed else round(r.stance, 3),
                "hostility": None if r.failed else round(r.hostility, 3),
                "conflict": (round(r.conflict, 3)
                             if not r.failed and np.isfinite(r.conflict) else None),
                "us_them": r.us_them, "confidence": round(r.confidence, 3),
                "off_topic": r.off_topic or r.failed,
            }))
    for (it, _), a in zip(items, assigned):
        if a in (ASSIGN_NONE, ASSIGN_FAILED):
            new_items.append(dict(it, model={
                "topic": None, "stance": None, "hostility": None,
                "conflict": None, "us_them": None,
                "assign_failed": a == ASSIGN_FAILED,
            }))

    out = sample_path.with_name(sample_path.stem + "_v2model.json")
    out.write_text(json.dumps(dict(sample, items=sorted(
        new_items, key=lambda x: x["id"]), classifier="v2 titular+entradilla"),
        ensure_ascii=False, indent=1))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
