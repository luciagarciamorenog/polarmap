"""
Pruebas del núcleo estadístico con datos sintéticos de estructura CONOCIDA.
Si el método no recupera 1, 2 o 3 grupos cuando los hay, no sirve.

    python3 validate/pilot/test_stats.py
"""
import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stats import (bradley_terry, correspondence, detect_groups,  # noqa: E402
                   group_polarization, ranking_to_pairs, standardize)

checks = []
rng = np.random.default_rng(7)


def check(desc, ok):
    checks.append((desc, bool(ok)))
    print(f"  [{'OK' if ok else 'FALLO'}] {desc}")


print("=== Grupos: ¿recupera la estructura que hay? ===")
N = 150
cases = {
    "consenso (1 grupo)": (rng.normal(0, 1, N), 1),
    "dos bloques 60/40": (np.r_[rng.normal(-1.5, .4, 90), rng.normal(1.5, .4, 60)], 2),
    "tres bloques": (np.r_[rng.normal(-2, .3, 50), rng.normal(0, .3, 50), rng.normal(2, .3, 50)], 3),
    "campana ancha (1 grupo)": (rng.uniform(-1, 1, N) + rng.normal(0, .5, N), 1),
}
scores = {}
for name, (x, k_true) in cases.items():
    g = detect_groups(standardize(x))
    s = group_polarization(g)
    scores[name] = s["score"]
    check(f"{name}: detecta {g.k} (esperado {k_true}) · puntuación {s['score']:.2f} · "
          f"cohesión {s['cohesion']:.2f} · distancia {s['distance']:.2f} DT", g.k == k_true)
check("consenso puntúa 0", scores["consenso (1 grupo)"] == 0)
check("dos bloques puntúa alto (>0,6)", scores["dos bloques 60/40"] > 0.6)

g = detect_groups(standardize(np.r_[rng.normal(0, 1, 140), rng.normal(6, .1, 10)]))
check(f"un puñado de outliers (≤10 %) no forma un bloque: k={g.k}", g.k == 1)

print("\n=== Bradley-Terry: ¿recupera el orden desde comparaciones ruidosas? ===")
n = 120
true = np.r_[rng.normal(-1.2, .35, 70), rng.normal(1.2, .35, 50)]
pairs = []
for _ in range(6):                      # cada noticia en ~6 conjuntos de 5
    perm = rng.permutation(n)
    for s in range(0, n - 4, 5):
        items = perm[s:s + 5]
        noisy = true[items] + rng.normal(0, .5, 5)   # juez imperfecto
        pairs += ranking_to_pairs(list(items[np.argsort(noisy)]))
theta = bradley_terry(n, pairs)
rho = spearmanr(theta, true).statistic
check(f"orden recuperado: Spearman {rho:.2f} (>0,85)", rho > 0.85)
g = detect_groups(standardize(theta))
check(f"dos bloques reales → detecta {g.k} con tamaños {np.round(g.sizes, 2)}", g.k == 2)

print("\n=== Correspondencias: ¿separa dos ecosistemas de medios? ===")
bloc_a = rng.multinomial(40, [.5, .3, .1, .1], size=12)
bloc_b = rng.multinomial(40, [.1, .1, .3, .5], size=10)
ca = correspondence(np.r_[bloc_a, bloc_b].astype(float))
axis = ca["rows"][:, 0]
sep = abs(axis[:12].mean() - axis[12:].mean()) / axis.std()
check(f"el eje 1 separa los dos bloques ({sep:.1f} DT) y explica "
      f"{ca['inertia_share'][0]:.0%} de la variación", sep > 1.5)
check("detecta 2 bloques de medios", detect_groups(standardize(axis)).k == 2)

fallos = [d for d, ok in checks if not ok]
print("\nRESULTADO:", "TODO PASA" if not fallos else f"FALLOS: {len(fallos)}")
raise SystemExit(1 if fallos else 0)
