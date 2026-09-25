"""
Piloto metodológico de PolarMap: tres dimensiones MECE sobre un día real.

  Ideológica  juicio comparativo (conjuntos de 5) → Bradley-Terry → grupos
              por BIC → Esteban-Ray sobre grupos. Dos rondas independientes
              de juicios para medir estabilidad.
  Afectiva    medición directa (hostilidad del lenguaje + nosotros/ellos)
              condicionada a los grupos ideológicos.
  Narrativa   encuadre dominante de cada noticia del día → tabla
              medio × encuadre → correspondencias → bloques de medios.

Para cada dimensión informa: número de grupos (y en qué % de réplicas
bootstrap sale cada k), tamaños, cohesión, distancia, incertidumbre y
puntuación. No combina las dimensiones ni fija pesos.

Uso:
    python3 validate/pilot/run_pilot.py            # llama a la API (con caché)
    python3 validate/pilot/run_pilot.py --mock     # jueces simulados, sin API
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest.leads import load_cache  # noqa: E402
from stats import (bradley_terry, correspondence, detect_groups,  # noqa: E402
                   group_polarization, ranking_to_pairs, standardize, summarize)
import judges  # noqa: E402

SET_SIZE = 5
APPEARANCES = 6          # veces que aparece cada noticia por ronda
MIN_OUTLET_ITEMS = 8     # medios con menos noticias no entran en la red
BOOT = 200


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40]


# ─────────────────────────────────────────────────────────────
# Datos
# ─────────────────────────────────────────────────────────────

def load_items(run: str, respect_tdm: bool) -> tuple[list[dict], list[dict], int]:
    rows = [json.loads(l) for l in
            (ROOT / f"data/internal/{run}_headlines.jsonl").read_text().splitlines()]
    full = json.loads((ROOT / f"data/internal/{run}_full.json").read_text())
    debates = [t["axis"] for c in full["raw_topic_results"].values() for t in c
               if t.get("axis")]
    leads = load_cache()
    items, n_tdm = [], 0
    for r in rows:
        ld = leads.get(r["url"], {})
        if respect_tdm and ld.get("tdm_reserved"):
            n_tdm += 1
            continue
        title = ld.get("title") if ld.get("status") == "ok" and ld.get("title") else r["title"]
        lead = ld.get("description", "") if ld.get("status") == "ok" else ""
        items.append({"url": r["url"], "domain": r["domain"], "topic": r["topic"],
                      "title": title, "text": f"{title}. {lead}" if lead else title,
                      "has_lead": bool(lead), "pipe_stance": r.get("stance"),
                      "pipe_hostility": r.get("hostility")})
    return items, debates, n_tdm


def load_reference() -> dict[str, dict]:
    """Etiquetas de referencia por URL: set de plata v2 y humano (30)."""
    sample = json.loads((ROOT / "data/gold/sample_ES_20260925.json").read_text())
    debates = [d["label"] for d in sample["debates"]]
    silver = json.loads((ROOT / "data/gold/silver_ES_20260925_v2.json").read_text())
    human = {}
    for f in (ROOT / "data/gold/labels_ES_20260925/labels").glob("*.json"):
        d = json.loads(f.read_text())
        human[f.stem] = d.get("data", d)
    ref = {}
    for it in sample["items"]:
        s = silver.get(it["id"], {})
        h = human.get(it["id"], {})
        topic = lambda l: debates[l["topic"]] if isinstance(l.get("topic"), int) else None
        ref[it["url"]] = {
            "silver_topic": topic(s), "silver_stance": s.get("stance"),
            "silver_hostility": s.get("hostility"), "silver_conflict": s.get("conflict"),
            "human_topic": topic(h) if h else None, "human_stance": h.get("stance"),
        }
    return ref


# ─────────────────────────────────────────────────────────────
# Jueces simulados (--mock): SOLO para probar la tubería
# ─────────────────────────────────────────────────────────────

class Mock:
    rng = np.random.default_rng(99)

    @classmethod
    def rank_sets(cls, sets, pole_a, pole_b, lookup):
        return [list(np.argsort([lookup.get(t, 0) + cls.rng.normal(0, .4) for t in s]))
                for s in sets]

    @classmethod
    def classify(cls, texts, pole_a, pole_b, lookup):
        return [{"hostility": float(np.clip(lookup.get(t, 0) or 0, 0, 1)),
                 "us_them": cls.rng.choice(["no", "political", "social_groups"])}
                for t in texts]

    @classmethod
    def code_frames(cls, texts, domains):
        out = []
        for t, d in zip(texts, domains):
            h = int(hashlib.md5(d.encode()).hexdigest(), 16)
            p = np.full(6, .1)
            p[h % 6] += .5
            out.append({"frame": list(judges.FRAMES)[cls.rng.choice(6, p=p / p.sum())],
                        "conflict": float(cls.rng.choice([0, .5, 1]))})
        return out


# ─────────────────────────────────────────────────────────────
# Dimensión 1 · Ideológica
# ─────────────────────────────────────────────────────────────

def make_sets(n: int, seed: int) -> list[list[int]]:
    rng = np.random.default_rng(seed)
    sets = []
    for _ in range(APPEARANCES):
        perm = list(rng.permutation(n))
        for s in range(0, n, SET_SIZE):
            chunk = perm[s:s + SET_SIZE]
            if len(chunk) >= 3:
                sets.append([int(i) for i in chunk])
    return sets


def ideological(debate, ditems, cache: Path, mock: bool, ref) -> dict:
    n = len(ditems)
    texts = [it["text"] for it in ditems]
    rounds = {}
    for rnd, seed in (("A", 1), ("B", 2)):
        sets = make_sets(n, seed)
        path = cache / f"rank_{slug(debate['label'])}_{rnd}.json"
        if mock:
            lookup = {it["text"]: (it["pipe_stance"] or 0) for it in ditems}
            orders = Mock.rank_sets([[texts[i] for i in s] for s in sets],
                                    debate["pole_a"], debate["pole_b"], lookup)
            orders = [[int(o) for o in od] for od in orders]
        else:
            orders = judges.load_or_run(path, judges.rank_sets,
                                        [[texts[i] for i in s] for s in sets],
                                        debate["pole_a"], debate["pole_b"])
        rounds[rnd] = [(s, o) for s, o in zip(sets, orders)]

    def fit(set_orders):
        pairs = []
        for s, o in set_orders:
            if o is not None:
                pairs += ranking_to_pairs([s[k] for k in o])
        return standardize(bradley_terry(n, pairs))

    all_sets = rounds["A"] + rounds["B"]
    n_invalid = sum(o is None for _, o in all_sets)
    z = fit(all_sets)
    za, zb = fit(rounds["A"]), fit(rounds["B"])
    g = detect_groups(z)
    pol = group_polarization(g)

    boot = defaultdict(list)
    rng = np.random.default_rng(0)
    for _ in range(BOOT):
        idx = rng.integers(0, len(all_sets), len(all_sets))
        gb = detect_groups(fit([all_sets[i] for i in idx]), seed=int(rng.integers(1e9)))
        pb = group_polarization(gb)
        boot["k"].append(gb.k)
        for key in ("score", "cohesion", "distance"):
            boot[key].append(pb[key])

    # Validación contra referencia (solo noticias de la muestra)
    val = {}
    for key in ("silver_stance", "human_stance"):
        pairs = [(z[i], ref[it["url"]][key]) for i, it in enumerate(ditems)
                 if it["url"] in ref and isinstance(ref[it["url"]].get(key), (int, float))
                 and ref[it["url"]].get(key.replace("stance", "topic")) == debate["label"]]
        if len(pairs) >= 5:
            a, b = np.array(pairs).T
            val[key] = {"n": len(pairs), "spearman": float(spearmanr(a, b).statistic)}

    exemplars = []
    for gi in range(g.k):
        members = np.where(g.labels == gi)[0]
        closest = members[np.argsort(np.abs(z[members] - g.means[gi]))[:3]]
        exemplars.append([ditems[i]["title"] for i in closest])

    return {
        "debate": debate["label"], "pole_a": debate["pole_a"], "pole_b": debate["pole_b"],
        "n": n, "judgments": len(all_sets), "invalid_judgments": n_invalid,
        "k": g.k, "bic": g.bic, "dip_p": g.dip_p,
        "sizes": g.sizes.tolist(), "means": g.means.tolist(), "sds": g.sds.tolist(),
        **pol,
        "k_share": {k: boot["k"].count(k) / BOOT for k in (1, 2, 3)},
        "score_ci": summarize(boot["score"]),
        "cohesion_ci": summarize(boot["cohesion"]),
        "distance_ci": summarize(boot["distance"]),
        "stability_spearman": float(spearmanr(za, zb).statistic),
        "validation": val, "exemplars": exemplars,
        "_z": z.tolist(), "_labels": g.labels.tolist(),
    }


# ─────────────────────────────────────────────────────────────
# Dimensión 2 · Afectiva (condicionada a los grupos ideológicos)
# ─────────────────────────────────────────────────────────────

def affective(debate, ditems, ideo, cache: Path, mock: bool, ref) -> dict:
    texts = [it["text"] for it in ditems]
    path = cache / f"affect_{slug(debate['label'])}.json"
    if mock:
        lookup = {it["text"]: it["pipe_hostility"] for it in ditems}
        res = Mock.classify(texts, debate["pole_a"], debate["pole_b"], lookup)
    else:
        from analysis.discover import classify_stances

        def run():
            return [{"hostility": None if r.failed else r.hostility, "us_them": r.us_them}
                    for r in classify_stances(texts, debate["pole_a"], debate["pole_b"])]
        res = judges.load_or_run(path, run)

    h = np.array([r["hostility"] if r["hostility"] is not None else np.nan for r in res])
    directed = np.array([h[i] if r.get("us_them") not in (None, "no") else 0.0
                         for i, r in enumerate(res)])
    labels = np.array(ideo["_labels"])
    ok = np.isfinite(h)

    def compute(sel):
        if ideo["k"] < 2:
            return 0.0, [float(np.nanmean(directed[sel]))]
        per = [float(np.nanmean(directed[sel & (labels == gi)])) if np.any(sel & (labels == gi))
               else 0.0 for gi in range(ideo["k"])]
        return float(np.dot(ideo["sizes"], per)), per

    score, per_group = compute(ok)
    rng = np.random.default_rng(1)
    boot_scores = []
    for _ in range(BOOT):
        idx = rng.integers(0, len(h), len(h))
        sel = np.zeros(len(h), bool)
        sel[np.unique(idx)] = True
        boot_scores.append(compute(sel & ok)[0])

    val = {}
    pairs = [(h[i], ref[it["url"]]["silver_hostility"]) for i, it in enumerate(ditems)
             if it["url"] in ref and ok[i] and ref[it["url"]].get("silver_hostility") is not None]
    if len(pairs) >= 5:
        a, b = np.array(pairs).T
        rho = spearmanr(a, b).statistic
        if np.isfinite(rho):          # sin variación en la referencia no hay correlación
            val["silver_hostility"] = {"n": len(pairs), "spearman": float(rho)}

    return {
        "debate": debate["label"], "n": int(ok.sum()),
        "score": score, "score_ci": summarize(boot_scores),
        "directed_hostility_by_group": per_group,
        "asymmetry": (max(per_group) - min(per_group)) if len(per_group) > 1 else 0.0,
        "general_hostility": float(np.nanmean(h)),
        "us_them_share": float(np.mean([r.get("us_them") not in (None, "no") for r in res])),
        "validation": val, "_h": np.nan_to_num(h).tolist(),
    }


# ─────────────────────────────────────────────────────────────
# Dimensión 3 · Narrativa (red de medios × encuadres)
# ─────────────────────────────────────────────────────────────

def narrative(items, cache: Path, mock: bool) -> dict:
    texts = [it["text"] for it in items]
    if mock:
        coded = Mock.code_frames(texts, [it["domain"] for it in items])
    else:
        coded = judges.load_or_run(cache / "frames.json", judges.code_frames, texts)
    frames = list(judges.FRAMES)
    rows = [(it["domain"], c) for it, c in zip(items, coded) if c]
    n_invalid = sum(c is None for c in coded)
    counts = Counter(d for d, _ in rows)
    outlets = sorted(d for d, n in counts.items() if n >= MIN_OUTLET_ITEMS)
    oi = {d: i for i, d in enumerate(outlets)}

    def table(sample_rows):
        t = np.full((len(outlets), len(frames)), 0.5)     # suavizado
        for d, c in sample_rows:
            if d in oi:
                t[oi[d], frames.index(c["frame"])] += 1
        return t

    ca = correspondence(table(rows))
    axis = standardize(ca["rows"][:, 0])
    g = detect_groups(axis)
    pol = group_polarization(g)

    rng = np.random.default_rng(2)
    boot = defaultdict(list)
    for _ in range(BOOT):
        sample = [rows[i] for i in rng.integers(0, len(rows), len(rows))]
        cb = correspondence(table(sample))
        gb = detect_groups(standardize(cb["rows"][:, 0]), seed=int(rng.integers(1e9)))
        pb = group_polarization(gb)
        boot["k"].append(gb.k)
        boot["score"].append(pb["score"])
        boot["inertia"].append(float(cb["inertia_share"][0]))

    col = ca["cols"][:, 0]
    order = np.argsort(col)
    conflict = defaultdict(list)
    frame_mix = defaultdict(Counter)
    for d, c in rows:
        if d in oi:
            conflict[d].append(c["conflict"])
            frame_mix[d][c["frame"]] += 1
    blocs = []
    for gi in range(g.k):
        members = [outlets[i] for i in np.where(g.labels == gi)[0]]
        mix = sum((frame_mix[m] for m in members), Counter())
        tot = sum(mix.values())
        blocs.append({"outlets": members,
                      "frame_mix": {f: mix[f] / tot for f in frames},
                      "conflict_mean": float(np.mean([x for m in members for x in conflict[m]]))})
    return {
        "n_items": len(rows), "invalid": n_invalid, "n_outlets": len(outlets),
        "axis_inertia": float(ca["inertia_share"][0]),
        "axis_inertia_ci": summarize(boot["inertia"]),
        "axis_frames": {"extremo_negativo": [frames[i] for i in order[:2]],
                        "extremo_positivo": [frames[i] for i in order[-2:]]},
        "k": g.k, "bic": g.bic, "dip_p": g.dip_p,
        "sizes": g.sizes.tolist(), "means": g.means.tolist(), "sds": g.sds.tolist(),
        **pol,
        "k_share": {k: boot["k"].count(k) / BOOT for k in (1, 2, 3)},
        "score_ci": summarize(boot["score"]),
        "blocs": blocs,
        "_outlet_axis": dict(zip(outlets, axis.tolist())),
        "_outlet_conflict": {d: float(np.mean(v)) for d, v in conflict.items()},
    }


# ─────────────────────────────────────────────────────────────
# ¿Miden cosas distintas?
# ─────────────────────────────────────────────────────────────

def distinctness(debate_items, ideo_res, aff_res, narr) -> dict:
    out = {}
    ext, host = [], []
    by_outlet = defaultdict(lambda: {"ext": [], "host": []})
    for ditems, ir, ar in zip(debate_items, ideo_res, aff_res):
        z, h = np.array(ir["_z"]), np.array(ar["_h"])
        ext += list(np.abs(z))
        host += list(h)
        for it, e, hh in zip(ditems, np.abs(z), h):
            by_outlet[it["domain"]]["ext"].append(e)
            by_outlet[it["domain"]]["host"].append(hh)
    out["noticia: extremismo ideológico vs hostilidad"] = {
        "n": len(ext), "spearman": float(spearmanr(ext, host).statistic)}
    common = [d for d in narr["_outlet_axis"] if len(by_outlet[d]["ext"]) >= 3]
    if len(common) >= 5:
        na = [abs(narr["_outlet_axis"][d]) for d in common]
        ie = [np.mean(by_outlet[d]["ext"]) for d in common]
        ah = [np.mean(by_outlet[d]["host"]) for d in common]
        nc = [narr["_outlet_conflict"][d] for d in common]
        out["medio: extremismo ideológico vs posición narrativa"] = {
            "n": len(common), "spearman": float(spearmanr(ie, na).statistic)}
        out["medio: hostilidad vs posición narrativa"] = {
            "n": len(common), "spearman": float(spearmanr(ah, na).statistic)}
        out["medio: hostilidad vs conflictividad"] = {
            "n": len(common), "spearman": float(spearmanr(ah, nc).statistic)}
    return out


# ─────────────────────────────────────────────────────────────
# Informe
# ─────────────────────────────────────────────────────────────

def pct(x):
    return "—" if x is None else f"{x:.0%}"


def fmt_ci(ci, digits=2):
    if ci["median"] is None:
        return "—"
    return f"{ci['median']:.{digits}f} [{ci['lo']:.{digits}f}–{ci['hi']:.{digits}f}]"


def report(res: dict, mock: bool) -> str:
    L = []
    L.append(f"# Piloto PolarMap · {res['country']} · {res['ts']}\n")
    if mock:
        L.append("> **SIMULACIÓN**: jueces simulados, sin API. Los números solo prueban "
                 "la tubería; no significan nada sobre España.\n")
    L.append(f"{res['n_items']} noticias del día ({res['n_tdm']} fuera por reserva TDM; "
             f"{pct(res['lead_share'])} con titular real + entradilla). "
             f"Incertidumbre: bootstrap de {BOOT} réplicas, intervalo del 90 %.\n")

    L.append("## 1 · Ideológica (juicio comparativo)\n")
    L.append("| Debate | n | Grupos (réplicas con 1/2/3) | Tamaños | Cohesión | Distancia (DT) | Puntuación | Estabilidad A vs B | Validez (Spearman) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in res["ideological"]:
        ks = "/".join(pct(r["k_share"][k]) for k in (1, 2, 3))
        val = " · ".join(f"{k.split('_')[0]} {v['spearman']:.2f} (n={v['n']})"
                         for k, v in r["validation"].items()) or "—"
        L.append(f"| {r['debate']} | {r['n']} | **{r['k']}** ({ks}) | "
                 f"{' / '.join(pct(s) for s in r['sizes'])} | {fmt_ci(r['cohesion_ci'])} | "
                 f"{fmt_ci(r['distance_ci'])} | **{fmt_ci(r['score_ci'])}** | "
                 f"{r['stability_spearman']:.2f} | {val} |")
    L.append("")
    for r in res["ideological"]:
        L.append(f"**{r['debate']}** · A: {r['pole_a']} · B: {r['pole_b']} · "
                 f"juicios inválidos {r['invalid_judgments']}/{r['judgments']}")
        for gi, ex in enumerate(r["exemplars"]):
            L.append(f"- Grupo {gi + 1} ({pct(r['sizes'][gi])}, centro {r['means'][gi]:+.2f} DT): "
                     + " · ".join(f"«{t}»" for t in ex))
        L.append("")

    L.append("## 2 · Afectiva (condicionada a los grupos ideológicos)\n")
    L.append("| Debate | n | Hostilidad dirigida por grupo | Puntuación | Asimetría | Crispación general | Nosotros/ellos | Validez hostilidad |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in res["affective"]:
        val = " · ".join(f"{v['spearman']:.2f} (n={v['n']})" for v in r["validation"].values()) or "—"
        L.append(f"| {r['debate']} | {r['n']} | "
                 f"{' / '.join(f'{x:.2f}' for x in r['directed_hostility_by_group'])} | "
                 f"**{fmt_ci(r['score_ci'])}** | {r['asymmetry']:.2f} | "
                 f"{r['general_hostility']:.2f} | {pct(r['us_them_share'])} | {val} |")
    L.append("\nSi la dimensión ideológica encuentra un solo grupo, la afectiva vale 0 por "
             "definición (no hay bloques entre los que dirigir hostilidad); la crispación "
             "general se informa aparte.\n")

    n = res["narrative"]
    L.append("## 3 · Narrativa (medios × encuadres)\n")
    ks = "/".join(pct(n["k_share"][k]) for k in (1, 2, 3))
    L.append(f"- {n['n_outlets']} medios con ≥{MIN_OUTLET_ITEMS} noticias · "
             f"{n['n_items']} noticias codificadas ({n['invalid']} inválidas)")
    L.append(f"- Eje principal: explica {fmt_ci(n['axis_inertia_ci'])} de la variación; "
             f"separa {', '.join(n['axis_frames']['extremo_negativo'])} de "
             f"{', '.join(n['axis_frames']['extremo_positivo'])}")
    L.append(f"- Bloques: **{n['k']}** (réplicas con 1/2/3: {ks}) · tamaños "
             f"{' / '.join(pct(s) for s in n['sizes'])} · cohesión {n['cohesion']:.2f} · "
             f"distancia {n['distance']:.2f} DT · **puntuación {fmt_ci(n['score_ci'])}**\n")
    for gi, b in enumerate(n["blocs"]):
        top = sorted(b["frame_mix"].items(), key=lambda x: -x[1])[:3]
        L.append(f"- Bloque {gi + 1} ({len(b['outlets'])} medios, conflictividad "
                 f"{b['conflict_mean']:.2f}): {', '.join(f'{f} {v:.0%}' for f, v in top)} · "
                 f"{', '.join(b['outlets'][:8])}{'…' if len(b['outlets']) > 8 else ''}")
    L.append("")

    L.append("## ¿Miden cosas distintas?\n")
    L.append("Correlaciones bajas (|ρ| < 0,5) indican dimensiones distintas.\n")
    for k, v in res["distinctness"].items():
        L.append(f"- {k}: ρ = {v['spearman']:.2f} (n={v['n']})")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="snapshot_partial")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--tdm", choices=("respect", "ignore"), default="respect")
    args = ap.parse_args()
    if not args.mock:
        from pipeline_discover import load_env
        load_env()

    items, debates, n_tdm = load_items(args.run, args.tdm == "respect")
    full = json.loads((ROOT / f"data/internal/{args.run}_full.json").read_text())
    tag = full["ts"][:10].replace("-", "")
    cache = ROOT / "data/pilot" / (("MOCK_" if args.mock else "") + f"ES_{tag}")
    ref = load_reference()

    debate_items, ideo_res, aff_res = [], [], []
    for d in debates:
        ditems = [it for it in items if it["topic"] == d["label"]]
        print(f"[ideológica] {d['label']}: {len(ditems)} noticias")
        ir = ideological(d, ditems, cache, args.mock, ref)
        print(f"[afectiva]   {d['label']}")
        ar = affective(d, ditems, ir, cache, args.mock, ref)
        debate_items.append(ditems)
        ideo_res.append(ir)
        aff_res.append(ar)
    print(f"[narrativa]  {len(items)} noticias")
    narr = narrative(items, cache, args.mock)

    res = {"country": "ES", "ts": full["ts"], "n_items": len(items), "n_tdm": n_tdm,
           "lead_share": float(np.mean([it["has_lead"] for it in items])),
           "ideological": ideo_res, "affective": aff_res, "narrative": narr,
           "distinctness": distinctness(debate_items, ideo_res, aff_res, narr)}
    cache.mkdir(parents=True, exist_ok=True)
    clean = json.loads(json.dumps(res, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
    (cache / "results.json").write_text(json.dumps(clean, ensure_ascii=False, indent=1))
    (cache / "report.md").write_text(report(res, args.mock))
    if not args.mock:
        from analysis import runner
        u = runner.LEDGER.summary()
        (cache / "usage.json").write_text(json.dumps(u, indent=2, ensure_ascii=False))
        print(f"consumo real de esta corrida: {u['usd_total']:.4f} $ "
              "(las respuestas ya cacheadas en disco no vuelven a pagar)")
    print(f"→ {cache / 'report.md'}")


if __name__ == "__main__":
    main()
