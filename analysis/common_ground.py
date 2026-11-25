"""
Terreno común y diferencia de fondo de cada debate.

Para cada debate con datos suficientes, el modelo lee titulares (con
entradilla) que ya se puntuaron como claramente inclinados a cada lado y
responde dos cosas:

  1. Qué comparten los dos lados (un objetivo, un hecho o un valor que
     aparece en titulares de AMBOS), o null si no hay nada claro.
  2. Dónde está la diferencia de verdad, con su tipo:
       objetivos · medios · hechos · confianza

Todo lo que afirma tiene que citar los titulares que lo muestran. El código
lo comprueba antes de publicar (verify): los titulares citados existen, hay
al menos uno de cada lado y vienen de al menos dos medios. Además se pide dos
veces y solo se publica si las dos pasadas coinciden en lo esencial (si hay
terreno común o no, y el tipo de diferencia). Es una síntesis del modelo, no
una medición validada, y la web lo dice.

Uso en la corrida: pipeline_discover.py llama a add_common_ground().
Uso suelto, para una corrida ya hecha (sin repetirla):

    python3 -m analysis.common_ground snapshot_ES_20261124
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

from analysis import runner
from analysis.discover import DISCOVER_MODEL, _cached_system

log = logging.getLogger(__name__)

MODEL = DISCOVER_MODEL
SIDE = 0.5            # |postura| desde aquí, el titular se inclina claramente
PER_SIDE = 20         # titulares por lado que se le dan al modelo
MIN_PER_SIDE = 5      # por debajo, no se intenta
MIN_OUTLETS = 2       # lo citado tiene que venir de al menos 2 medios
PASSES = 2            # se pide dos veces; solo se publica si coinciden
TEMPERATURE = 0.2
LEAD_CHARS = 240
TYPES = ("objetivos", "medios", "hechos", "confianza")

# Webs de partidos o instituciones que GDELT mezcla con la prensa.
NOT_PRESS = {"pp.es", "psoe.es", "voxespana.es", "podemos.info",
             "movimientosumar.es", "lamoncloa.gob.es"}

SYSTEM = """Eres un analista de medios que ayuda a entender un debate público sin tomar partido.
Recibes titulares (con su entradilla) de medios que se inclinan hacia cada uno de los dos lados de un debate.

DEBATE: {label}
LADO A: {pole_a}
LADO B: {pole_b}

Tu tarea tiene dos partes.

1. LO QUE COMPARTEN. ¿Hay algo que aparezca en titulares de AMBOS lados y que los dos den por bueno? Puede ser:
   - un objetivo compartido (por ejemplo, que la vivienda sea asequible),
   - un hecho que ninguno discute (por ejemplo, que los precios han subido),
   - un valor común.
   Reglas:
   - Solo lo que está en los textos, no lo que supongas.
   - Cita los identificadores de los titulares que lo muestran: al menos uno del lado A (A…) y uno del lado B (B…).
   - Nada de frases que valdrían para cualquier debate ("ambos quieren lo mejor para el país", "a ambos les preocupa el tema"). Tiene que ser concreto de este debate.
   - Si no lo hay, responde null. Es una respuesta válida y mejor que forzarla.

2. DÓNDE ESTÁ LA DIFERENCIA DE VERDAD. El núcleo del desacuerdo, en una frase concreta, y su tipo:
   - "objetivos": quieren cosas distintas o priorizan valores distintos.
   - "medios": comparten el objetivo y discrepan en cómo conseguirlo.
   - "hechos": no coinciden en qué está pasando o en sus causas.
   - "confianza": discuten si un actor (Gobierno, partido, jueces, institución) es legítimo o de fiar, más que la política en sí.
   Cita titulares de ambos lados que lo muestren.

Redacción: neutral y simétrica, sin dar la razón a ningún lado y sin nombrar medios. Frases cortas (25 palabras como mucho), cada una en español y en inglés.

Responde SOLO con este JSON:
{{"shared_es": "..." o null, "shared_en": "..." o null, "shared_ids": ["A3", "B5"],
 "differ_es": "...", "differ_en": "...", "differ_type": "objetivos|medios|hechos|confianza", "differ_ids": ["A1", "B2"]}}"""


# ── Selección de titulares ───────────────────────────────────────────

def _text(r: dict) -> str:
    lead = (r.get("lead") or "").strip()
    if len(lead) > LEAD_CHARS:
        lead = lead[:LEAD_CHARS].rsplit(" ", 1)[0] + "…"
    title = (r.get("title") or "").strip()
    return f"{title}. {lead}" if lead else title


def _spread(rows: list[dict], n: int) -> list[dict]:
    """Los más claros primero, pero repartidos entre medios (uno de cada
    medio por ronda), para que no sea la voz de un solo periódico."""
    by_outlet: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (-abs(r["stance"]), -(r.get("confidence") or 0))):
        by_outlet[r["domain"]].append(r)
    out: list[dict] = []
    while len(out) < n and any(by_outlet.values()):
        for dom in sorted(by_outlet, key=lambda d: -len(by_outlet[d])):
            if by_outlet[dom] and len(out) < n:
                out.append(by_outlet[dom].pop(0))
    return out


def select_evidence(rows: list[dict], label: str,
                    per_side: int = PER_SIDE) -> dict[str, dict]:
    """{id: titular} con ids A1…An y B1…Bn."""
    scored = [r for r in rows
              if r.get("topic") == label and not r.get("off_topic")
              and isinstance(r.get("stance"), (int, float)) and r.get("title")
              and r.get("domain") not in NOT_PRESS]
    a = _spread([r for r in scored if r["stance"] <= -SIDE], per_side)
    b = _spread([r for r in scored if r["stance"] >= SIDE], per_side)
    ev = {f"A{i+1}": {**r, "side": "A"} for i, r in enumerate(a)}
    ev.update({f"B{i+1}": {**r, "side": "B"} for i, r in enumerate(b)})
    return ev


def build_body(axis: dict, ev: dict[str, dict]) -> dict:
    system = SYSTEM.format(label=axis.get("label", ""), pole_a=axis.get("pole_a", ""),
                           pole_b=axis.get("pole_b", ""))
    lines = ["LADO A — titulares:"]
    lines += [f"{k}. {_text(r)}" for k, r in ev.items() if r["side"] == "A"]
    lines += ["", "LADO B — titulares:"]
    lines += [f"{k}. {_text(r)}" for k, r in ev.items() if r["side"] == "B"]
    return {"model": MODEL, "max_tokens": 900, "temperature": TEMPERATURE,
            "system": _cached_system(system),
            "messages": [{"role": "user", "content": "\n".join(lines)}]}


# ── Lectura y comprobación ───────────────────────────────────────────

def parse(text: str | None) -> dict | None:
    """Primer objeto JSON de la respuesta (tolera ```json y texto extra)."""
    if not text:
        return None
    cleaned = re.sub(r"```(json)?", "", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _backed(ids, ev: dict[str, dict]) -> list[str] | None:
    """Los ids citados, si respaldan la afirmación: existen, hay al menos
    uno de cada lado y vienen de al menos MIN_OUTLETS medios."""
    if not isinstance(ids, list):
        return None
    ids = [i for i in dict.fromkeys(str(x).strip().upper() for x in ids) if i in ev]
    sides = {ev[i]["side"] for i in ids}
    outlets = {ev[i]["domain"] for i in ids}
    if sides != {"A", "B"} or len(outlets) < MIN_OUTLETS:
        return None
    return ids


def _clean(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = " ".join(s.split())
    return s or None


def verify(obj: dict | None, ev: dict[str, dict]) -> dict | None:
    """Lo que se puede publicar de una respuesta. None si es ilegible.
    shared/differ quedan en None si no tienen respaldo suficiente."""
    if obj is None:
        return None
    out = {"shared": None, "differ": None}
    es, en = _clean(obj.get("shared_es")), _clean(obj.get("shared_en"))
    ids = _backed(obj.get("shared_ids"), ev)
    if es and en and ids:
        out["shared"] = {"es": es, "en": en, "ids": ids}
    es, en = _clean(obj.get("differ_es")), _clean(obj.get("differ_en"))
    typ = str(obj.get("differ_type") or "").strip().lower()
    ids = _backed(obj.get("differ_ids"), ev)
    if es and en and typ in TYPES and ids:
        out["differ"] = {"es": es, "en": en, "type": typ, "ids": ids}
    return out


def consistent(passes: list[dict | None]) -> bool:
    """Las pasadas coinciden en lo esencial: todas legibles, todas con o
    todas sin terreno común, y el mismo tipo de diferencia."""
    if not passes or any(p is None for p in passes):
        return False
    has_shared = {p["shared"] is not None for p in passes}
    types = {(p["differ"] or {}).get("type") for p in passes}
    return len(has_shared) == 1 and len(types) == 1 and None not in types


def summarize(axis: dict, ev: dict[str, dict], passes: list[dict | None]) -> dict:
    n_a = sum(r["side"] == "A" for r in ev.values())
    n_b = len(ev) - n_a
    base = {"model": MODEL, "passes": len(passes), "n_a": n_a, "n_b": n_b,
            "outlets": len({r["domain"] for r in ev.values()})}
    if not consistent(passes):
        return {**base, "status": "unstable", "raw": passes}
    first = passes[0]
    cited = set((first["shared"] or {}).get("ids", [])) | set(first["differ"]["ids"])
    evidence = {i: {"side": ev[i]["side"], "title": ev[i]["title"],
                    "domain": ev[i]["domain"], "url": ev[i]["url"]}
                for i in sorted(cited)}
    return {**base, "status": "ok", "shared": first["shared"],
            "differ": first["differ"], "evidence": evidence}


# ── Para una corrida ────────────────────────────────────────────────

def add_common_ground(topic_results: dict[str, list[dict]], rows: list[dict],
                      mode: str = "batch", passes: int = PASSES) -> None:
    """Añade t["common_ground"] a cada debate con datos suficientes.
    Un fallo aquí no tumba la corrida: el debate se queda sin el bloque."""
    jobs = []   # (topic_result, evidence)
    for country, topics in topic_results.items():
        crow = [r for r in rows if r.get("country", country) == country]
        for t in topics:
            axis = t.get("axis") or {}
            if not t.get("trustworthy") or not axis.get("label"):
                continue
            ev = select_evidence(crow, axis["label"])
            n_a = sum(r["side"] == "A" for r in ev.values())
            if min(n_a, len(ev) - n_a) < MIN_PER_SIDE:
                t["common_ground"] = {"status": "insufficient", "n_a": n_a,
                                      "n_b": len(ev) - n_a}
                continue
            jobs.append((t, ev))
    if not jobs:
        return
    bodies = [build_body(t["axis"], ev) for t, ev in jobs for _ in range(passes)]
    try:
        texts = runner.run(bodies, "terreno_comun", mode)
    except Exception as e:  # noqa: BLE001 — el resto de la corrida vale igual
        log.error("  [terreno común] no se pudo calcular: %s", e)
        for t, _ in jobs:
            t["common_ground"] = {"status": "failed"}
        return
    for k, (t, ev) in enumerate(jobs):
        res = [verify(parse(x), ev) for x in texts[k * passes:(k + 1) * passes]]
        t["common_ground"] = summarize(t["axis"], ev, res)
        cg = t["common_ground"]
        log.info("  [terreno común] %s: %s", t["axis"]["label"], cg["status"])
        if cg["status"] == "ok":
            log.info("    comparten: %s", (cg["shared"] or {}).get("es") or "—")
            log.info("    difieren (%s): %s", cg["differ"]["type"], cg["differ"]["es"])


# ── Uso suelto ───────────────────────────────────────────────────────

def _main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(argv) != 1:
        sys.exit("Uso: python3 -m analysis.common_ground snapshot_ES_AAAAMMDD")
    root = Path(__file__).resolve().parents[1]
    internal = root / "data" / "internal"
    stem = argv[0].removesuffix(".json")
    full_p, head_p = internal / f"{stem}_full.json", internal / f"{stem}_headlines.jsonl"
    if not (full_p.exists() and head_p.exists()):
        sys.exit(f"No encuentro {full_p.name} y {head_p.name} en data/internal/")

    sys.path.insert(0, str(root))
    from pipeline_discover import load_env
    load_env()

    full = json.loads(full_p.read_text())
    rows = [json.loads(l) for l in head_p.read_text().splitlines() if l.strip()]
    runner.LEDGER.reset()
    add_common_ground(full["raw_topic_results"], rows, mode="sync")
    full_p.write_text(json.dumps(full, indent=2, ensure_ascii=False, default=str))
    usd = runner.LEDGER.summary()["usd_total"]

    print("\n" + "=" * 60)
    for topics in full["raw_topic_results"].values():
        for t in topics:
            cg = t.get("common_ground")
            if not cg:
                continue
            print(f"\n■ {t['axis']['label']}  [{cg['status']}]")
            if cg["status"] == "ok":
                sh, df = cg["shared"], cg["differ"]
                print(f"  Comparten: {sh['es'] if sh else 'No hay terreno común claro en los titulares de hoy.'}")
                print(f"  Se diferencian ({df['type']}): {df['es']}")
                for i, e in cg["evidence"].items():
                    print(f"    {i} [{e['domain']}] {e['title']}")
            elif cg["status"] == "unstable":
                print("  Las dos pasadas no coinciden: no se publicará. Lo que dijo cada una:")
                for k, p in enumerate(cg.get("raw") or [], 1):
                    if p is None:
                        print(f"    pasada {k}: respuesta ilegible")
                        continue
                    print(f"    pasada {k}: comparten = {(p['shared'] or {}).get('es')}")
                    print(f"               difieren ({(p['differ'] or {}).get('type')}) = {(p['differ'] or {}).get('es')}")
    print(f"\nCoste: {usd:.2f} $. Guardado en {full_p.name}.")
    print("Para verlo: python3 web/build_data.py && open web/index.html")


if __name__ == "__main__":
    _main(sys.argv[1:])
