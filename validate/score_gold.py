"""
Puntúa el pipeline contra el set de referencia etiquetado a mano (Nivel 3).

Entradas:
  - data/gold/sample_<país>_<fecha>.json   (muestra + etiquetas del modelo)
  - las etiquetas de referencia: un directorio con un JSON por titular
    (<dir>/labels/<id>.json, tal como lo exporta la página de etiquetado)
    o un único JSON {id: etiqueta} (el set de plata)

Mide:
  1. Asignación de debate: acuerdo, kappa de Cohen y precisión/recobrado
     por debate (incluido "ninguno").
  2. Qué contiene "ninguno": reparto de lo que el modelo dejó fuera,
     con intervalo de Wilson al 90 % y extrapolado al total del día.
  3. Postura: en los titulares donde humano y modelo coinciden de debate,
     correlación de Spearman, acuerdo de dirección y error medio.
  4. Hostilidad: correlación entre la escala humana (0/1/2) y la del modelo.
  5. «Nosotros contra ellos»: cuánto aparece, dentro y fuera de los debates
     (materia prima para la dimensión afectiva amplia).

Uso:
    python3 validate/score_gold.py data/gold/sample_ES_20260925.json \\
        data/gold/labels_ES_20260925
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

Z90 = 1.6449


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + Z90 ** 2 / n
    mid = (p + Z90 ** 2 / (2 * n)) / den
    half = Z90 * math.sqrt(p * (1 - p) / n + Z90 ** 2 / (4 * n * n)) / den
    return (mid - half, mid + half)


def kappa(a: list, b: list) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def load(sample_path: Path, labels_dir: Path):
    sample = json.loads(sample_path.read_text())
    debates = [d["label"] for d in sample["debates"]]
    labels = {}
    if labels_dir.is_file():
        # Un solo JSON {id: etiqueta} (p. ej. el set de plata)
        labels = json.loads(labels_dir.read_text())
    else:
        for f in sorted((labels_dir / "labels").glob("*.json")):
            doc = json.loads(f.read_text())
            labels[f.stem] = doc.get("data", doc)
    return sample, debates, labels


def human_topic(label: dict, debates: list[str]) -> str | None:
    t = label.get("topic")
    return debates[t] if isinstance(t, int) else None


def main(sample_path: str, labels_dir: str):
    sample, debates, labels = load(Path(sample_path), Path(labels_dir))
    items = [it for it in sample["items"] if it["id"] in labels
             and not labels[it["id"]].get("garbled")
             and labels[it["id"]].get("topic") is not None]
    n_garbled = sum(1 for l in labels.values() if l.get("garbled"))
    n_unsure = sum(1 for it in items if labels[it["id"]].get("unsure"))
    print(f"Muestra {Path(sample_path).name}: {len(sample['items'])} titulares | "
          f"etiquetados útiles {len(items)} | ilegibles {n_garbled} | dudosos {n_unsure}")
    if not items:
        return

    # 1. Asignación de debate
    NONE = "ninguno"
    h = [human_topic(labels[it["id"]], debates) or NONE for it in items]
    m = [it["model"]["topic"] or NONE for it in items]
    acc = sum(x == y for x, y in zip(h, m)) / len(h)
    print(f"\n1. ASIGNACIÓN DE DEBATE  acuerdo {acc:.0%} · kappa {kappa(h, m):.2f}")
    print(f"   {'debate':<46} {'precisión':>9} {'recobrado':>9}  (n humano / n modelo)")
    for d in debates + [NONE]:
        tp = sum(x == d and y == d for x, y in zip(h, m))
        nm, nh = m.count(d), h.count(d)
        prec = tp / nm if nm else float("nan")
        rec = tp / nh if nh else float("nan")
        print(f"   {d[:46]:<46} {prec:9.0%} {rec:9.0%}  ({nh} / {nm})")

    # 2. Qué contiene "ninguno" (según el modelo)
    none_items = [it for it in items if it["model"]["topic"] is None]
    cats = Counter()
    for it in none_items:
        l = labels[it["id"]]
        t = human_topic(l, debates)
        cats["debería estar en: " + t if t else
             {"no_stance": "sin postura posible",
              "political": "político, de otro asunto",
              "nonpolitical_controversy": "controversia no política",
              "other": "otro"}.get(l.get("none_type"), "sin tipo")] += 1
    pop = sample["population"]["none"]
    print(f"\n2. QUÉ CONTIENE «NINGUNO»  ({len(none_items)} de la muestra; "
          f"{pop} en el día)")
    for cat, k in cats.most_common():
        lo, hi = wilson(k, len(none_items))
        print(f"   {cat:<46} {k:3d}  {k / len(none_items):5.0%}  "
              f"IC90 [{lo:.0%}, {hi:.0%}]  ≈ {round(pop * k / len(none_items))} titulares/día")

    # 3. Postura
    pairs = [(labels[it["id"]]["stance"], it["model"]["stance"])
             for it in items
             if it["model"]["topic"] and human_topic(labels[it["id"]], debates) == it["model"]["topic"]
             and isinstance(labels[it["id"]].get("stance"), (int, float))
             and it["model"].get("stance") is not None
             and not it["model"].get("off_topic")]
    print(f"\n3. POSTURA  ({len(pairs)} titulares con el mismo debate y postura codificable)")
    if len(pairs) >= 5:
        hs, ms = np.array(pairs, dtype=float).T
        rho = spearmanr(hs, ms).statistic
        sign = lambda x: np.where(x < -0.2, -1, np.where(x > 0.2, 1, 0))
        agree = np.mean(sign(hs) == sign(ms))
        print(f"   Spearman {rho:.2f} · misma dirección (A/centro/B) {agree:.0%} · "
              f"error medio {np.mean(np.abs(hs - ms)):.2f} en escala -1..1")
        print(f"   media humana {hs.mean():+.2f} · media modelo {ms.mean():+.2f} · "
              f"sd humana {hs.std():.2f} · sd modelo {ms.std():.2f}")
        print(f"   en el centro (|x|≤0,2): humano {np.mean(np.abs(hs) <= .2):.0%} · "
              f"modelo {np.mean(np.abs(ms) <= .2):.0%}")

    # 4. Hostilidad
    hp = [(labels[it["id"]]["hostility"], it["model"]["hostility"]) for it in items
          if labels[it["id"]].get("hostility") is not None
          and it["model"].get("hostility") is not None]
    print(f"\n4. HOSTILIDAD  ({len(hp)} titulares con valor en ambos)")
    if len(hp) >= 5:
        hh, mh = np.array(hp, dtype=float).T
        print(f"   Spearman {spearmanr(hh, mh).statistic:.2f}")
        for lvl, name in enumerate(["ninguna", "algo", "alta"]):
            sel = mh[hh == lvl]
            if sel.size:
                print(f"   humano «{name}»: modelo medio {sel.mean():.2f} (n={sel.size})")

    # 4b. Conflictividad (libro de códigos v2)
    cp = [(labels[it["id"]]["conflict"], it["model"]["conflict"]) for it in items
          if labels[it["id"]].get("conflict") is not None
          and it["model"].get("conflict") is not None]
    if len(cp) >= 5:
        hc, mc = np.array(cp, dtype=float).T
        print(f"\n4b. CONFLICTIVIDAD  ({len(cp)} titulares) Spearman "
              f"{spearmanr(hc, mc).statistic:.2f}")
        for lvl, name in enumerate(["ninguna", "algo", "alta"]):
            sel = mc[hc == lvl]
            if sel.size:
                print(f"   humano «{name}»: modelo medio {sel.mean():.2f} (n={sel.size})")

    ut = [(labels[it["id"]]["us_them"], it["model"]["us_them"]) for it in items
          if labels[it["id"]].get("us_them") and it["model"].get("us_them")]
    if len(ut) >= 5:
        a, b = zip(*ut)
        print(f"\n4c. NOSOTROS/ELLOS modelo vs referencia ({len(ut)}): acuerdo "
              f"{np.mean([x == y for x, y in ut]):.0%} · kappa {kappa(list(a), list(b)):.2f}")

    # 5. Nosotros contra ellos
    print("\n5. «NOSOTROS CONTRA ELLOS»")
    for scope, subset in [("en debates (modelo)", [it for it in items if it["model"]["topic"]]),
                          ("en «ninguno» (modelo)", none_items)]:
        c = Counter(labels[it["id"]].get("us_them") or "sin respuesta" for it in subset)
        total = sum(c.values())
        parts = " · ".join(f"{k} {v / total:.0%}" for k, v in c.most_common())
        print(f"   {scope:<24} {parts}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
