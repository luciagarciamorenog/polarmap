"""
Pruebas de analysis/ecosystem.py con datos sintéticos (sin red ni API).

    cd analysis && python3 test_ecosystem.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.ecosystem import (  # noqa: E402
    agenda_report, axis_report, best_two_split, ecosystem_report, genre_from_url,
)


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(1)


def rows_for(outlet_means, n, noise, topic="t", seed=0, field="stance"):
    rng = np.random.default_rng(seed)
    out = []
    for dom, mu in outlet_means.items():
        for k, s in enumerate(np.clip(mu + rng.normal(0, noise, n), -1, 1)):
            out.append({"domain": dom, "topic": topic, field: float(s),
                        "hostility": 0.1, "country": "ES",
                        "url": f"https://{dom}/politica/x-{k}.html"})
    return out


print("ecosystem")

# Dos bandos de medios claros → distancia muy por encima del azar
two = rows_for({**{f"a{i}": -0.6 for i in range(5)}, **{f"b{i}": 0.6 for i in range(5)}},
               n=10, noise=0.4)
r = axis_report(two, n_perm=500)
check("dos bandos: p < 0.01", r["camps"]["p"] < 0.01)
check("dos bandos: exceso sobre el azar > 0,5", r["camps"]["excess_over_chance"] > 0.5)
check("dos bandos: bando A son los a*",
      {d for d, *_ in r["camps"]["left"]} == {f"a{i}" for i in range(5)})

# Todos los medios iguales → nada por encima del azar
same = rows_for({f"m{i}": 0.0 for i in range(10)}, n=10, noise=0.4, seed=1)
r0 = axis_report(same, n_perm=500)
check("medios iguales: p > 0.05", r0["camps"]["p"] > 0.05)

# Un medio, un voto: un medio enorme y extremo pesa menos
big = rows_for({"grande": 0.9}, n=200, noise=0.05) + rows_for(
    {f"m{i}": 0.0 for i in range(6)}, n=5, noise=0.05, seed=2)
rb = axis_report(big, n_perm=100)
check("un medio, un voto cambia el índice", rb["er_outlet_weighted"] != rb["er_headline"])

check("partición en dos: distancia correcta",
      abs(best_two_split(np.array([-1, -0.9, 0.9, 1.0]))[0] - 1.9) < 1e-9)

# Opinión por la URL
check("opinión por URL", genre_from_url("https://elpais.com/opinion/2026-11-20/x.html") == "opinion")
check("noticia por URL", genre_from_url("https://elpais.com/espana/2026-11-20/x.html") == "news")
check("la palabra en el slug no cuenta",
      genre_from_url("https://x.es/politica/la-opinion-de-feijoo.html") == "news")

# Agenda: medios que cubren debates distintos frente a todos iguales
rng = np.random.default_rng(3)
div = [{"domain": f"o{i}", "topic": ("T1" if i < 3 else "T2") if rng.random() < 0.9 else "T3"}
       for i in range(6) for _ in range(30)]
check("agendas distintas: p < 0.01", agenda_report(div, n_perm=300)["p"] < 0.01)
flat = [{"domain": f"o{i}", "topic": rng.choice(["T1", "T2", "T3"])}
        for i in range(6) for _ in range(30)]
check("agendas iguales: p > 0.05", agenda_report(flat, n_perm=300)["p"] > 0.05)

# Informe completo: sin eje Gobierno ↔ oposición lo dice, con él lo mide
rep = ecosystem_report(two)
check("sin bloc → nota", "note" in rep["bloc"])
rep2 = ecosystem_report(two + rows_for({f"a{i}": -0.5 for i in range(5)} | {f"b{i}": 0.5 for i in range(5)},
                                       n=6, noise=0.3, topic="t2", field="bloc"))
check("con bloc → bandos medidos", "camps" in rep2["bloc"])

print("\nRESULTADO: TODO PASA")
