"""
Tests del terreno común (analysis/common_ground.py). Sin red, sin API.
    python3 analysis/test_common_ground.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import common_ground as cg  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        fails += 1


def row(i, stance, domain, topic="Vivienda"):
    return {"country": "ES", "topic": topic, "off_topic": False, "stance": stance,
            "confidence": 0.8, "domain": domain, "title": f"Titular {i}",
            "lead": "Entradilla " * 40, "url": f"https://x/{i}"}


rows = ([row(i, -0.8, f"izq{i % 3}.es") for i in range(30)]
        + [row(100 + i, 0.7, f"der{i % 4}.es") for i in range(30)]
        + [row(200 + i, 0.0, "centro.es") for i in range(10)]          # neutros: fuera
        + [row(300, -1.0, "psoe.es"), row(301, 1.0, "pp.es")]           # partidos: fuera
        + [row(400 + i, -0.9, "otro.es", topic="Migración") for i in range(5)])

print("selección")
ev = cg.select_evidence(rows, "Vivienda")
check("20 por lado", sum(r["side"] == "A" for r in ev.values()) == 20
      and sum(r["side"] == "B" for r in ev.values()) == 20)
check("sin neutros, sin webs de partidos ni otros debates",
      all(r["domain"] not in {"centro.es", "psoe.es", "pp.es", "otro.es"} for r in ev.values()))
check("repartidos entre medios", len({r["domain"] for r in ev.values() if r["side"] == "A"}) == 3)
check("la entradilla se recorta", len(cg._text(ev["A1"])) < 300)
body = cg.build_body({"label": "Vivienda", "pole_a": "Intervenir", "pole_b": "Construir"}, ev)
check("el cuerpo lleva los dos lados", "A1." in body["messages"][0]["content"]
      and "B1." in body["messages"][0]["content"] and "LADO A: Intervenir" in body["system"][0]["text"])

print("comprobación")
a_ids = [k for k in ev if k.startswith("A")]
b_ids = [k for k in ev if k.startswith("B")]
a_other = next(k for k in a_ids if ev[k]["domain"] != ev[a_ids[0]]["domain"])
good = {"shared_es": "Que la vivienda es cara.", "shared_en": "That housing is expensive.",
        "shared_ids": [a_ids[0], b_ids[0]],
        "differ_es": "Regular o construir.", "differ_en": "Regulate or build.",
        "differ_type": "medios", "differ_ids": [a_other, b_ids[1]]}
v = cg.verify(cg.parse("```json\n" + json.dumps(good) + "\n```"), ev)
check("respuesta válida pasa (y se lee aunque venga entre ```)", v["shared"] and v["differ"]
      and v["differ"]["type"] == "medios")
v = cg.verify({**good, "shared_ids": [a_ids[0], a_ids[1]]}, ev)
check("terreno común con citas de un solo lado se rechaza", v["shared"] is None and v["differ"])
v = cg.verify({**good, "shared_ids": ["A1", "B99"]}, ev)
check("ids inventados se rechazan", v["shared"] is None)
ev2 = {k: dict(r) for k, r in ev.items()}
ev2[b_ids[0]]["domain"] = ev2[a_ids[0]]["domain"]
check("un solo medio no basta, aunque cite los dos lados", cg._backed([a_ids[0], b_ids[0]], ev2) is None)
v = cg.verify({**good, "differ_type": "ideología"}, ev)
check("tipo de diferencia fuera de la lista se rechaza", v["differ"] is None)
v = cg.verify({**good, "shared_es": None, "shared_en": None, "shared_ids": []}, ev)
check("'no hay terreno común' es una respuesta válida", v["shared"] is None and v["differ"])
check("respuesta ilegible → None", cg.verify(cg.parse("no sé"), ev) is None)

print("estabilidad")
p1 = cg.verify(good, ev)
p2 = cg.verify({**good, "shared_es": "Otra redacción.", "shared_en": "Other wording."}, ev)
check("misma esencia con otras palabras = estable", cg.consistent([p1, p2]))
p3 = cg.verify({**good, "differ_type": "hechos"}, ev)
check("distinto tipo de diferencia = inestable", not cg.consistent([p1, p3]))
p4 = cg.verify({**good, "shared_es": None, "shared_en": None}, ev)
check("una con terreno común y otra sin él = inestable", not cg.consistent([p1, p4]))
check("una pasada ilegible = inestable", not cg.consistent([p1, None]))
s = cg.summarize({"label": "Vivienda"}, ev, [p1, p2])
check("resumen: ok, con los titulares citados", s["status"] == "ok"
      and set(s["evidence"]) == set(good["shared_ids"]) | set(good["differ_ids"]))
check("resumen inestable no publica texto", cg.summarize({}, ev, [p1, p3])["status"] == "unstable")

print("corrida")
topics = {"ES": [{"axis": {"label": "Vivienda", "pole_a": "A", "pole_b": "B"}, "trustworthy": True},
                 {"axis": {"label": "Migración", "pole_a": "A", "pole_b": "B"}, "trustworthy": True},
                 {"axis": {"label": "Otro", "pole_a": "A", "pole_b": "B"}, "trustworthy": False}]}
calls = []


def fake_run(bodies, stage, mode):
    calls.append((len(bodies), stage))
    return [json.dumps(good)] * len(bodies)


cg.runner.run = fake_run
cg.add_common_ground(topics, rows, mode="sync")
t_viv, t_mig, t_otro = topics["ES"]
check("una petición por pasada, solo para debates con datos", calls == [(2, "terreno_comun")])
check("debate fiable con datos → ok", t_viv["common_ground"]["status"] == "ok")
check("debate con un solo lado → insuficiente", t_mig["common_ground"]["status"] == "insufficient")
check("debate sin datos suficientes → sin bloque", "common_ground" not in t_otro)


def broken_run(bodies, stage, mode):
    raise RuntimeError("sin red")


t2 = {"ES": [{"axis": {"label": "Vivienda", "pole_a": "A", "pole_b": "B"}, "trustworthy": True}]}
cg.runner.run = broken_run
cg.add_common_ground(t2, rows)
check("si la API falla, la corrida sigue", t2["ES"][0]["common_ground"]["status"] == "failed")

print()
if fails:
    print(f"RESULTADO: {fails} FALLOS")
    sys.exit(1)
print("RESULTADO: TODAS LAS ASERCIONES PASAN")
