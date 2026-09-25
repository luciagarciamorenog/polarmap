"""
Núcleo estadístico del piloto (sin dependencias fuera de numpy/scipy).

La polarización es una propiedad del CONJUNTO de noticias: las posiciones
salen de comparar noticias entre sí, los grupos se detectan sin fijar su
número y la puntuación se calcula a partir de tamaños y distancias de esos
grupos. Cada función hace una cosa que se puede explicar en una frase.

    bradley_terry        comparaciones «A está más cerca del polo B que C»
                         → una posición por noticia
    detect_groups        ¿1, 2 o 3 grupos? mezcla gaussiana 1D + BIC
    group_polarization   tamaños y distancias → puntuación Esteban-Ray
    correspondence       tabla medio × encuadre → posición de cada medio
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:
    import diptest as _diptest
except ImportError:          # pragma: no cover
    _diptest = None


# ─────────────────────────────────────────────────────────────
# 1. Posiciones relativas: Bradley-Terry
# ─────────────────────────────────────────────────────────────

def ranking_to_pairs(order: list[int]) -> list[tuple[int, int]]:
    """Un orden de A→B se rompe en pares (ganador, perdedor), donde
    «gana» la noticia más cercana al polo B."""
    return [(order[j], order[i])
            for i in range(len(order)) for j in range(i + 1, len(order))]


def bradley_terry(n: int, pairs: list[tuple[int, int]],
                  prior: float = 0.5, iters: int = 2000,
                  tol: float = 1e-9) -> np.ndarray:
    """
    Posición latente de cada noticia (log-fuerza, centrada en 0).

    Algoritmo MM de Hunter (2004). `prior` añade a cada noticia media
    victoria y media derrota frente a una referencia neutra, para que una
    noticia que siempre gana no se vaya a infinito.
    """
    wins = np.zeros((n, n))
    for w, l in pairs:
        wins[w, l] += 1
    games = wins + wins.T
    total_wins = wins.sum(axis=1) + prior
    p = np.ones(n)
    for _ in range(iters):
        denom = (games / (p[:, None] + p[None, :])).sum(axis=1) + 2 * prior / (p + 1.0)
        new = total_wins / denom
        new /= np.exp(np.mean(np.log(new)))      # fija la escala
        if np.max(np.abs(np.log(new) - np.log(p))) < tol:
            p = new
            break
        p = new
    theta = np.log(p)
    return theta - theta.mean()


def standardize(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 0 else x * 0.0


# ─────────────────────────────────────────────────────────────
# 2. ¿Cuántos grupos? Mezcla gaussiana 1D + BIC
# ─────────────────────────────────────────────────────────────

@dataclass
class Groups:
    k: int
    sizes: np.ndarray             # proporción de noticias por grupo
    means: np.ndarray             # centro de cada grupo (en DT de la escala)
    sds: np.ndarray               # dispersión interna de cada grupo
    labels: np.ndarray            # grupo de cada noticia
    bic: dict = field(default_factory=dict)       # BIC por k probado
    dip_p: float = float("nan")   # test de Hartigan: p<0,05 → más de un modo


def _gmm1d(x: np.ndarray, k: int, rng: np.random.Generator,
           restarts: int = 12, iters: int = 300):
    n = len(x)
    var_floor = max(1e-4, 0.05 ** 2 * x.var())
    best = None
    for _ in range(restarts):
        mu = np.sort(rng.choice(x, k, replace=False)) if k > 1 else np.array([x.mean()])
        sd = np.full(k, x.std() / k if k > 1 else x.std()) + 1e-6
        w = np.full(k, 1.0 / k)
        ll_old = -np.inf
        for _ in range(iters):
            dens = (w / (sd * math.sqrt(2 * math.pi))) * np.exp(
                -0.5 * ((x[:, None] - mu) / sd) ** 2)
            tot = dens.sum(axis=1, keepdims=True) + 1e-300
            resp = dens / tot
            ll = float(np.log(tot).sum())
            nk = resp.sum(axis=0) + 1e-12
            w = nk / n
            mu = (resp * x[:, None]).sum(axis=0) / nk
            sd = np.sqrt(np.maximum((resp * (x[:, None] - mu) ** 2).sum(axis=0) / nk,
                                    var_floor))
            if ll - ll_old < 1e-8:
                break
            ll_old = ll
        if best is None or ll > best[0]:
            best = (ll, w, mu, sd, resp)
    return best


def detect_groups(x: np.ndarray, kmax: int = 3, min_share: float = 0.10,
                  seed: int = 0) -> Groups:
    """
    Prueba 1, 2 y 3 grupos y se queda con el de menor BIC. Una solución con
    algún grupo por debajo de `min_share` de las noticias no cuenta como
    grupo (sería un puñado de casos raros, no un bloque).
    """
    x = np.asarray(x, dtype=float)
    rng = np.random.default_rng(seed)
    n = len(x)
    fits, bic = {}, {}
    for k in range(1, min(kmax, max(1, n // 5)) + 1):
        ll, w, mu, sd, resp = _gmm1d(x, k, rng)
        if k > 1 and w.min() < min_share:
            continue
        bic[k] = -2 * ll + (3 * k - 1) * math.log(n)
        fits[k] = (w, mu, sd, resp)
    k = min(bic, key=bic.get)
    w, mu, sd, resp = fits[k]
    order = np.argsort(mu)
    remap = np.empty(k, dtype=int)
    remap[order] = np.arange(k)
    dip_p = float(_diptest.diptest(x)[1]) if (_diptest and n >= 4) else float("nan")
    return Groups(k=k, sizes=w[order], means=mu[order], sds=sd[order],
                  labels=remap[resp.argmax(axis=1)], bic=bic, dip_p=dip_p)


# ─────────────────────────────────────────────────────────────
# 3. Puntuación: Esteban-Ray sobre los grupos detectados
# ─────────────────────────────────────────────────────────────

def group_polarization(g: Groups, alpha: float = 1.0) -> dict:
    """
    P = Σᵢ Σⱼ πᵢ^(1+α) πⱼ |μᵢ − μⱼ|, con posiciones en desviaciones típicas.

    Con la escala estandarizada, dos grupos iguales sin dispersión interna
    quedan en ±1 y dan P = 0,5: la puntuación es 2P, en [0, 1]
    (1 = dos bloques iguales y totalmente compactos; 0 = un solo grupo).

    Devuelve también las piezas que la explican:
      cohesion  proporción de la variación que explican los grupos (R²)
      distance  separación entre los dos grupos más grandes, en DT
    """
    pi, mu = g.sizes, g.means
    P = float(sum(pi[i] ** (1 + alpha) * pi[j] * abs(mu[i] - mu[j])
                  for i in range(g.k) for j in range(g.k)))
    total_var = float(np.sum(pi * (g.sds ** 2 + (mu - np.sum(pi * mu)) ** 2)))
    between = float(np.sum(pi * (mu - np.sum(pi * mu)) ** 2))
    top2 = np.argsort(pi)[-2:] if g.k > 1 else [0]
    return {
        "score": min(1.0, 2 * P),
        "P": P,
        "cohesion": between / total_var if total_var > 0 else 0.0,
        "distance": float(abs(mu[top2[-1]] - mu[top2[0]])) if g.k > 1 else 0.0,
    }


def summarize(values: list[float]) -> dict:
    """Mediana e intervalo del 90 % de una distribución bootstrap."""
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)])
    if v.size == 0:
        return {"median": None, "lo": None, "hi": None}
    return {"median": float(np.median(v)), "lo": float(np.percentile(v, 5)),
            "hi": float(np.percentile(v, 95))}


# ─────────────────────────────────────────────────────────────
# 4. Análisis de correspondencias (medios × encuadres)
# ─────────────────────────────────────────────────────────────

def correspondence(table: np.ndarray, dims: int = 2) -> dict:
    """
    Sitúa filas (medios) y columnas (encuadres) en un mismo mapa según lo
    que se parecen sus perfiles. `inertia` es la parte de la variación que
    explica cada eje: si el primero explica poco, no hay una división clara.
    """
    P = table / table.sum()
    r, c = P.sum(axis=1), P.sum(axis=0)
    S = (P - np.outer(r, c)) / np.sqrt(np.outer(r, c))
    U, sv, Vt = np.linalg.svd(S, full_matrices=False)
    inertia = sv ** 2
    rows = (U[:, :dims] * sv[:dims]) / np.sqrt(r)[:, None]
    cols = (Vt.T[:, :dims] * sv[:dims]) / np.sqrt(c)[:, None]
    return {"rows": rows, "cols": cols,
            "inertia_share": inertia[:dims] / inertia.sum()}
