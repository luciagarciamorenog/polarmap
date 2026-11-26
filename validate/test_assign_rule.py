"""
Prueba A/B del paso de asignación sobre la muestra de referencia.

Asigna los titulares de la muestra con las instrucciones actuales (A) y con
la regla de la campaña (B), mismo modelo, mismos lotes y mismos debates del
día, y compara ambos con las etiquetas de referencia. No toca la corrida
diaria ni la web. Coste: céntimos (Haiku 4.5).

Uso:
    python3 validate/test_assign_rule.py data/gold/sample_ES_20261125.json \\
        data/gold/labels_gemini_todas_ES_20261125.json
"""
from __future__ import annotations
import json, os, sys, time
from collections import Counter
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.discover import build_assign_bodies, parse_assign, GOV_LABEL  # noqa: E402

def api_key() -> str:
    """Igual que el pipeline (pipeline_discover.load_env): la clave de .env
    gana sobre la exportada en la terminal, que puede ser una vieja."""
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip().removeprefix("export ").strip() == "ANTHROPIC_API_KEY" and v.strip():
                return v.strip().strip('"').strip("'")
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k: return k
    raise SystemExit("Falta ANTHROPIC_API_KEY")

def call(body: dict, key: str) -> tuple[str | None, dict]:
    for intento in range(4):
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"}, json=body)
        if r.status_code == 200:
            j = r.json()
            return "".join(c.get("text", "") for c in j["content"]), j.get("usage", {})
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(5 * (intento + 1)); continue
        raise SystemExit(f"HTTP {r.status_code}: {r.text[:300]}")
    return None, {}

def main(sample_path: str, labels_path: str):
    sample = json.loads(Path(sample_path).read_text())
    run = sample["source_run"]["run"]
    full = json.loads(Path(f"data/internal/{run}_full.json").read_text())
    debates = [t["axis"] for t in full["raw_topic_results"][sample["country"]] if t.get("axis")]
    assert [d["label"] for d in debates] == [d["label"] for d in sample["debates"]]
    items = sample["items"]
    texts = [f"{it['title']}. {it['lead']}" if it["lead"] else it["title"] for it in items]
    key = api_key()
    out, cost_in, cost_out = {}, 0, 0
    for name, rule in (("A_actual", False), ("B_regla", True), ("B2_regla", "b2")):
        bodies = build_assign_bodies(texts, debates, campaign_rule=rule)
        resp = []
        for body, n in bodies:
            t, u = call(body, key); resp.append(t)
            cost_in += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            cost_out += u.get("output_tokens", 0)
        ks = parse_assign(resp, bodies, len(debates))
        out[name] = [debates[k - 1]["label"] if k > 0 else ("ninguno" if k == 0 else "fallo") for k in ks]
    usd = cost_in / 1e6 * 1.0 + cost_out / 1e6 * 5.0   # Haiku 4.5: 1 $/M entrada, 5 $/M salida
    Path(f"data/gold/assign_ab_{sample['country']}_{run[-8:]}.json").write_text(json.dumps(
        {"items": [it["id"] for it in items], **out}, ensure_ascii=False, indent=1))

    ref = json.loads(Path(labels_path).read_text())
    labs = [d["label"] for d in sample["debates"]]
    extras = [d["label"] for d in sample.get("extra_debates", [])]
    def r_lab(l):
        t = l["topic"]
        if t == "none": return "ninguno"
        return labs[t] if t < len(labs) else "extra:otros debates del catálogo"
    R = [r_lab(ref[it["id"]]) for it in items]
    Rcmp = ["ninguno" if x.startswith("extra:") else x for x in R]   # el modelo no tenía esos debates
    print(f"Coste de la prueba ≈ {usd:.3f} $ ({cost_in} tokens de entrada, {cost_out} de salida)\n")
    names = list(out)
    print(f"{'':34}" + "".join(f"{n:>14}" for n in names))
    print(f"{'Acierto total (asignación)':34}" + "".join(
        f"{sum(a == b for a, b in zip(out[n], Rcmp)) / len(items):14.1%}" for n in names))
    for d in labs + ["ninguno"]:
        cells = []
        for n in names:
            tp = sum(a == d and b == d for a, b in zip(out[n], Rcmp))
            nm, nh = out[n].count(d), Rcmp.count(d)
            cells.append(f"{(tp/nm if nm else float('nan')):.0%}/{(tp/nh if nh else float('nan')):.0%}")
        print(f"  {d[:32]:32}" + "".join(f"{c:>14}" for c in cells) + f"   (precisión/recobrado; ref {Rcmp.count(d)})")
    for n in names[1:]:
        print(f"\nTitulares que cambian de A a {n}:")
        for it, a, b, r in zip(items, out["A_actual"], out[n], R):
            if a != b:
                rc = "ninguno" if r.startswith("extra:") else r
                ok = "✓" if b == rc else ("✗" if a == rc else "·")
                print(f"  {ok} {it['id']}: {a[:22]} → {b[:22]} | ref {r[:30]} | {it['title'][:80]}")
    for n in names[1:]:
        print(f"\n{n} manda a un debate de política lo que la referencia tiene en Gobierno:")
        for it, b, r in zip(items, out[n], R):
            if b not in (GOV_LABEL, "ninguno", "fallo") and r == GOV_LABEL:
                print(f"  {it['id']}: {b} | {it['title'][:90]}")
        print(f"{n} manda a Gobierno lo que la referencia tiene en un debate de política activo:")
        for it, b, r in zip(items, out[n], R):
            if b == GOV_LABEL and r in labs and r != GOV_LABEL:
                print(f"  {it['id']}: ref {r} | {it['title'][:90]}")
    print("\nReferencia en «otros debates del catálogo»:", sum(1 for x in R if x.startswith("extra:")),
          "| dónde los pone cada versión:", {n: dict(Counter(b for b, x in zip(out[n], R) if x.startswith("extra:"))) for n in names})


if __name__ == "__main__":
    main(*sys.argv[1:3])
