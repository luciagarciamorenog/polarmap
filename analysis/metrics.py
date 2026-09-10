"""
Métricas de polarización.

Distinción clave que hace este módulo:
  - DISPERSIÓN  = cuánto se separan las opiniones del centro (varianza)
  - POLARIZACIÓN = cuánto se agrupan en bloques separados y hostiles

Una distribución uniforme tiene dispersión ALTA y polarización BAJA.
Dos picos en los extremos tienen dispersión similar y polarización ALTA.
Confundir ambas es el error #1 en dashboards de "polarización".
"""

from __future__ import annotations

import numpy as np
from scipy import stats

try:
    import diptest as _diptest
    _HAS_DIPTEST = True
except ImportError:
    _HAS_DIPTEST = False

# Cache del ancla de normalización (se calcula una sola vez por config)
_REF_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 1. ESTEBAN-RAY  --  el índice canónico de polarización en la literatura
# ---------------------------------------------------------------------------

def _reference_max(alpha: float, n_bins: int, edges: np.ndarray,
                   centers: np.ndarray, dist: np.ndarray) -> float:
    """
    Ancla de normalización REALISTA, no teórica.

    El máximo teórico (dos masas puntuales en -1 y +1) es inalcanzable con
    datos reales: cualquier distribución empírica se reparte entre varios
    bins, y como pi va elevado a (1+alpha), esa dispersión hunde el valor.
    Normalizar contra el teórico comprime todo el índice a [0, 0.15] y lo
    hace inútil como escala pública.

    Usamos como 100% un escenario de polarización severa pero REAL:
    dos bloques gaussianos del mismo tamaño centrados en -0.85 y +0.85
    con sd=0.10. Eso es "sociedad partida en dos" observable.
    Valores >1 son posibles y significan polarización peor que ese ancla;
    se recortan a 1 en la capa de presentación, no aquí.
    """
    rng = np.random.default_rng(0)

    def _er_of(sample):
        hist, _ = np.histogram(np.clip(sample, -1, 1), bins=edges)
        pi = hist / hist.sum()
        return float((pi ** (1.0 + alpha)) @ dist @ pi)

    # Techo (=1.0): dos bloques del mismo tamaño en -0.85 y +0.85
    ceiling = _er_of(np.concatenate([
        rng.normal(-0.85, 0.10, 200_000),
        rng.normal(+0.85, 0.10, 200_000),
    ]))

    # Suelo (=0.0): consenso compacto. ER no es cero para una masa única
    # porque los bins adyacentes conservan distancia > 0 y pi^(1+alpha) es
    # grande cuando la masa está concentrada. Sin restar este suelo, una
    # sociedad en consenso total puntuaría ~0.15 de polarización, que es
    # sencillamente falso.
    floor = _er_of(rng.normal(0.0, 0.05, 200_000))

    return {"floor": floor, "ceiling": ceiling}


def esteban_ray(
    positions: np.ndarray,
    alpha: float = 1.0,
    n_bins: int = 21,
    weights: np.ndarray | None = None,
    normalize: bool = True,
) -> float:
    """
    Índice de polarización de Esteban & Ray (1994).

        P(alpha) = K * sum_i sum_j  pi_i^(1+alpha) * pi_j * |y_i - y_j|

    Captura formalmente la idea de "identificación con el propio grupo +
    alienación respecto al otro". Un grupo grande y compacto lejos de otro
    grupo grande y compacto = polarización máxima. Muchos grupos pequeños
    dispersos = polarización baja aunque la varianza sea alta.

    Args:
        positions: array de posiciones en [-1, 1] (una por documento/usuario)
        alpha: sensibilidad a la identificación intragrupo. La literatura usa
               [1.0, 1.6]. alpha=0 degenera en la desviación media de Gini.
        n_bins: discretización del eje de posiciones.
        weights: peso por observación (p.ej. alcance/engagement del post).
        normalize: escala el resultado a [0, 1] dividiendo por el máximo
                   teórico (dos masas de 0.5 en los extremos).

    Returns:
        float en [0, 1] si normalize=True.
    """
    positions = np.asarray(positions, dtype=float)
    positions = positions[np.isfinite(positions)]
    if positions.size < 2:
        return float("nan")

    if weights is None:
        weights = np.ones_like(positions)
    else:
        weights = np.asarray(weights, dtype=float)[np.isfinite(positions)]

    edges = np.linspace(-1.0, 1.0, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0

    hist, _ = np.histogram(np.clip(positions, -1, 1), bins=edges, weights=weights)
    total = hist.sum()
    if total <= 0:
        return float("nan")
    pi = hist / total

    # Producto matricial: sum_i sum_j pi_i^(1+a) pi_j |y_i - y_j|
    dist = np.abs(centers[:, None] - centers[None, :])
    p_alpha = pi ** (1.0 + alpha)
    er = float(p_alpha @ dist @ pi)

    if normalize:
        key = (alpha, n_bins)
        if key not in _REF_CACHE:
            _REF_CACHE[key] = _reference_max(alpha, n_bins, edges, centers, dist)
        ref = _REF_CACHE[key]
        span = ref["ceiling"] - ref["floor"]
        if span <= 0:
            return float("nan")
        # Calibración de dos puntos: 0 = consenso, 1 = sociedad partida en dos.
        # No recortamos por arriba: >1 significa peor que el escenario ancla
        # y es información legítima. El recorte se hace al pintar el mapa.
        er = (er - ref["floor"]) / span
        er = max(er, 0.0)

    return er


# ---------------------------------------------------------------------------
# 2. BIMODALIDAD  --  ¿hay dos bloques o una campana?
# ---------------------------------------------------------------------------

def hartigan_dip(positions: np.ndarray) -> dict:
    """
    Test de Hartigan: H0 = la distribución es unimodal.

    p < 0.05  ->  se rechaza unimodalidad -> hay estructura multimodal.
    Es la prueba estadística formal de "existen dos bandos".
    """
    positions = np.asarray(positions, dtype=float)
    positions = positions[np.isfinite(positions)]
    if positions.size < 10:
        return {"dip": float("nan"), "p_value": float("nan"), "is_multimodal": None}

    if not _HAS_DIPTEST:
        raise ImportError("pip install diptest")

    dip, pval = _diptest.diptest(positions)
    return {
        "dip": float(dip),
        "p_value": float(pval),
        "is_multimodal": bool(pval < 0.05),
    }


def bimodality_coefficient(positions: np.ndarray) -> float:
    """
    Coeficiente de bimodalidad de Sarle.

        BC = (skew^2 + 1) / (kurtosis + 3*(n-1)^2 / ((n-2)(n-3)))

    BC > 0.555 (valor de la uniforme) sugiere bimodalidad.
    Barato de calcular; úsalo como filtro rápido y confirma con dip test.
    """
    x = np.asarray(positions, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 4:
        return float("nan")

    skew = stats.skew(x, bias=False)
    kurt = stats.kurtosis(x, fisher=True, bias=False)  # exceso de curtosis
    denom = kurt + 3.0 * ((n - 1) ** 2) / ((n - 2) * (n - 3))
    if denom <= 0:
        return float("nan")
    return float((skew ** 2 + 1.0) / denom)


# ---------------------------------------------------------------------------
# 3. POLARIZACIÓN AFECTIVA  --  hostilidad dirigida al otro bloque
# ---------------------------------------------------------------------------

def affective_polarization(
    positions: np.ndarray,
    hostility: np.ndarray,
    threshold: float = 0.33,
) -> dict:
    """
    Hostilidad condicionada a la posición.

    No basta con "hay mucho lenguaje tóxico". Lo que indica polarización
    afectiva es que la hostilidad se concentra en los extremos y va dirigida
    al otro bloque. Un debate duro pero con hostilidad plana en todo el
    espectro es otra cosa (crispación general, no polarización).

    Args:
        positions: posiciones en [-1, 1]
        hostility: score de hostilidad/toxicidad en [0, 1] (p.ej. Perspective API)
        threshold: umbral para considerar una posición "extrema"

    Returns:
        dict con hostilidad media por bloque y el ratio extremos/centro.
    """
    pos = np.asarray(positions, dtype=float)
    hos = np.asarray(hostility, dtype=float)
    mask = np.isfinite(pos) & np.isfinite(hos)
    pos, hos = pos[mask], hos[mask]
    if pos.size < 10:
        return {k: float("nan") for k in
                ("hostility_left", "hostility_right", "hostility_center",
                 "extremity_ratio", "affective_index")}

    left = hos[pos <= -threshold]
    right = hos[pos >= threshold]
    center = hos[np.abs(pos) < threshold]

    h_left = float(left.mean()) if left.size else float("nan")
    h_right = float(right.mean()) if right.size else float("nan")
    h_center = float(center.mean()) if center.size else float("nan")

    extremes = np.concatenate([left, right])

    # Fracción de la población que ocupa el centro. Un centro que se vacía es
    # en sí mismo el indicador más limpio de polarización: no hay puente.
    center_share = float(center.size / pos.size)

    if not extremes.size:
        # Todo el mundo en el centro -> no hay polarización afectiva
        return {"hostility_left": h_left, "hostility_right": h_right,
                "hostility_center": h_center, "extremity_ratio": 0.0,
                "center_share": center_share, "affective_index": 0.0}

    if center.size >= 10 and center.mean() > 1e-6:
        ratio = float(extremes.mean() / center.mean())
    else:
        # Centro vacío o casi: NO es dato faltante, es la señal más fuerte.
        ratio = float("inf")

    # Índice afectivo: hostilidad media en los extremos, amplificada cuando
    # esa hostilidad es específica de los extremos (ratio alto) y no un ruido
    # de fondo que afecta a todo el espectro por igual (ratio ~1 = crispación
    # general, no polarización).
    base = float(extremes.mean())
    if np.isinf(ratio):
        lift = 1.0
    else:
        lift = float(np.clip((ratio - 1.0) / 2.0, 0.0, 1.0))

    # Penalización adicional por centro vacío: refuerza la señal estructural
    emptiness = float(np.clip(1.0 - center_share / 0.34, 0.0, 1.0))
    lift = max(lift, emptiness)

    affective = float(np.clip(base * (0.4 + 0.6 * lift), 0.0, 1.0))

    return {
        "hostility_left": h_left,
        "hostility_right": h_right,
        "hostility_center": h_center,
        "extremity_ratio": ratio,
        "center_share": center_share,
        "affective_index": affective,
    }


# ---------------------------------------------------------------------------
# 4. CONTROLES  --  lo que evita falsos positivos
# ---------------------------------------------------------------------------

def dispersion(positions: np.ndarray) -> float:
    """Desviación típica. Se reporta SIEMPRE junto a ER para poder distinguir
    dispersión de polarización. Si ER es alto y dispersión también, ok.
    Si dispersión es alta y ER bajo -> hay debate plural, no dos bandos."""
    x = np.asarray(positions, dtype=float)
    x = x[np.isfinite(x)]
    return float(x.std(ddof=1)) if x.size > 1 else float("nan")


def effective_sample_size(weights: np.ndarray) -> float:
    """
    N efectivo de Kish. Si 5 cuentas generan el 90% del volumen, tu muestra
    no vale 10.000 documentos: vale ~5. Esto evita que una campaña
    coordinada dispare el índice de un país.
    """
    w = np.asarray(weights, dtype=float)
    w = w[np.isfinite(w) & (w > 0)]
    if w.size == 0:
        return 0.0
    return float(w.sum() ** 2 / (w ** 2).sum())


# ---------------------------------------------------------------------------
# 5. ÍNDICE COMPUESTO
# ---------------------------------------------------------------------------

def polarization_profile(
    positions: np.ndarray,
    hostility: np.ndarray | None = None,
    weights: np.ndarray | None = None,
    min_n: int = 100,
) -> dict:
    """
    Perfil completo para un (país, tema, ventana temporal).

    Devuelve las tres dimensiones por separado + un compuesto.
    NUNCA colapses esto a un solo número en la UI sin poder desglosarlo:
    es lo que separa una herramienta seria de un termómetro de humor.
    """
    pos = np.asarray(positions, dtype=float)
    n = int(np.isfinite(pos).sum())

    out = {
        "n": n,
        "n_effective": effective_sample_size(weights) if weights is not None else float(n),
        "sufficient_data": n >= min_n,
    }

    if n < min_n:
        out.update({k: float("nan") for k in
                    ("ideological", "affective", "dispersion",
                     "bimodality_coef", "dip_p", "composite")})
        return out

    er = esteban_ray(pos, weights=weights)
    dip = hartigan_dip(pos)
    bc = bimodality_coefficient(pos)

    out.update({
        "ideological": er,
        "dispersion": dispersion(pos),
        "bimodality_coef": bc,
        "dip_p": dip["p_value"],
        "is_multimodal": dip["is_multimodal"],
    })

    if hostility is not None:
        aff = affective_polarization(pos, hostility)
        out.update(aff)
        affective = aff["affective_index"]
    else:
        affective = float("nan")
        out["affective_index"] = affective

    # Compuesto: media geométrica de ideológica y afectiva.
    # Geométrica y no aritmética a propósito: si una dimensión es ~0,
    # el compuesto debe caer. Dos bandos que no se odian no son el mismo
    # fenómeno que dos bandos que se odian.
    if np.isfinite(er) and np.isfinite(affective):
        out["composite"] = float(np.sqrt(max(er, 0) * max(affective, 0)))
    else:
        out["composite"] = float(er) if np.isfinite(er) else float("nan")

    return out
