"""
Tests del contrato v1 (CONTRACT.md). Sin red, sin API.

    cd analysis && python3 test_contract.py
"""
import json

from contract import (
    V1_COUNTRIES, build_country, build_snapshot, dumps, freeze_axis,
    normalize_label, short_label, topic_is_trust,
)

checks = []


def check(desc, ok):
    checks.append((desc, bool(ok)))
    print(f"  [{'OK' if ok else 'FALLO'}] {desc}")


def topic(label="Seguridad pública y orden", n=176, ne=164.36,
          n_domains=51, hhi=0.05, top_share=0.10,
          ideo=0.26066974, aff=0.30821917, comp=0.28344913,
          status="ok"):
    return {
        "status": status,
        "axis": {"label": label, "pole_a": "Polo menos",
                 "pole_b": "Polo más"},
        "n_articles": n + 20,
        "n_classified": n,
        "n_effective": ne,
        "metrics": {"ideological": ideo, "affective_index": aff,
                    "composite": comp},
        "source_quality": {"n_domains": n_domains, "hhi": hhi,
                           "top_share": top_share},
    }


print("=== TEMA: criterio trust (§3) ===")
check("tema sano es trust", topic_is_trust(topic()))
check("n=79 no es trust", not topic_is_trust(topic(n=79)))
check("n=80 sí es trust", topic_is_trust(topic(n=80)))
check("ne=49.9 no es trust", not topic_is_trust(topic(ne=49.9)))
check("7 dominios no es trust", not topic_is_trust(topic(n_domains=7)))
check("hhi=0.25 no es trust (estricto)",
      not topic_is_trust(topic(hhi=0.25)))
check("top_share=0.40 no es trust (estricto)",
      not topic_is_trust(topic(top_share=0.40)))
check("métricas NaN no es trust",
      not topic_is_trust(topic(ideo=float("nan"))))
check("status distinto de ok no es trust",
      not topic_is_trust(topic(status="insufficient_after_filtering")))

print()
print("=== PAÍS: agregado y gris (§2, §3) ===")
c = build_country([topic()])
check("un tema n=176 pinta el país", c["trust"] and c["color"] == 0.283)
check("dom mix cuando ideo≈aff", c["dom"] == "mix")
check("ne del país es entero con floor", c["ne"] == 164)

c = build_country([topic(n=90, ne=60)])
check("un solo tema de 90 (<100) deja el país gris",
      not c["trust"] and c["color"] is None and c["dom"] is None)
check("...pero n/ne explican el gris", c["n"] == 90 and c["ne"] == 60)
check("...y el tema sí es trust en la ficha", c["topics"][0]["trust"])

c = build_country([topic(label=f"T{i}", n=40, ne=38) for i in range(3)])
check("tres temas de 40 (suma 120) siguen grises",
      not c["trust"] and c["n"] == 0 and c["ne"] == 0)
check("temas no trust sin números",
      all(t["ideo"] is None and t["comp"] is None for t in c["topics"]))

c = build_country([topic(label="A", n=100, ideo=0.6, aff=0.1, comp=0.24),
                   topic(label="B", n=300, ideo=0.2, aff=0.1, comp=0.14),
                   topic(label="C", n=40, ne=38, ideo=0.9, aff=0.9, comp=0.9)])
check("media ponderada por n solo con temas trust",
      c["color"] == round((0.24 * 100 + 0.14 * 300) / 400, 3))
check("dom ideo cuando ideo > aff*1.2", c["dom"] == "ideo")
check("fiables primero, ordenados por comp",
      [t["l"] for t in c["topics"]] == ["A", "B", "C"])

c = build_country([topic(label=f"T{i}", n=100 + i) for i in range(7)])
check("máximo 5 temas", len(c["topics"]) == 5)

c = build_country([{"status": "no_clear_debate", "n_articles": 50},
                   {"status": "insufficient_data", "n_articles": 3}])
check("temas sin eje no se listan", c["topics"] == [] and not c["trust"])

print()
print("=== SNAPSHOT: forma y serialización (§2) ===")
snap = build_snapshot({"ES": [topic()], "XX": [topic()]},
                      ts="2026-09-10T12:00:00Z", window_hours=24)
check("siempre los 10 países v1, nada más",
      list(snap["countries"]) == list(V1_COUNTRIES))
check("país sin resultados va en gris",
      snap["countries"]["CL"] == {"trust": False, "color": None,
                                  "dom": None, "n": 0, "ne": 0,
                                  "topics": []})
check("raíz v/ts/w", (snap["v"], snap["ts"], snap["w"])
      == (1, "2026-09-10T12:00:00Z", 24))
s = dumps(snap)
check("compacto, sin espacios ni saltos", "\n" not in s and ", " not in s
      and '": ' not in s)
check("tildes sin escapar", "Seguridad pública" in s)
t = json.loads(s)["countries"]["ES"]["topics"][0]
check("floats a 3 decimales", (t["ideo"], t["aff"], t["comp"])
      == (0.261, 0.308, 0.283))
check("claves exactas del tema", set(t) ==
      {"l", "a", "n", "ne", "ideo", "aff", "comp", "trust"})
check("claves exactas del país", set(json.loads(s)["countries"]["ES"]) ==
      {"trust", "color", "dom", "n", "ne", "topics"})
try:
    dumps({"x": float("nan")})
    check("NaN se rechaza al serializar", False)
except ValueError:
    check("NaN se rechaza al serializar", True)

print()
print("=== ETIQUETAS Y EJES ===")
check("etiqueta ≤ 48 chars", len(short_label("x" * 80)) == 48)
check("etiqueta corta intacta", short_label("Vivienda") == "Vivienda")
check("normalizar quita acentos, mayúsculas y puntuación",
      normalize_label("  Política  migratoria, ¡YA! ")
      == normalize_label("politica migratoria ya"))
reg = {}
a1 = freeze_axis({"label": "Política migratoria", "pole_a": "Cerrar",
                  "pole_b": "Abrir"}, reg)
a2 = freeze_axis({"label": "politica MIGRATORIA", "pole_a": "Acoger",
                  "pole_b": "Restringir"}, reg)
check("primer eje se registra tal cual", a1["pole_a"] == "Cerrar")
check("mismo tema reutiliza polos y orientación",
      (a2["pole_a"], a2["pole_b"]) == ("Cerrar", "Abrir")
      and a2["label"] == "politica MIGRATORIA")

failed = [d for d, ok in checks if not ok]
print()
print("RESULTADO:", "TODO PASA" if not failed else f"FALLOS: {failed}")
raise SystemExit(1 if failed else 0)
