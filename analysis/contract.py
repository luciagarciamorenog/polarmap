"""
Contrato de datos v1 del mapa (ver docs/CONTRACT.md).

Todo lo que decide qué ve el frontend vive aquí y es puro: sin red,
sin API, sin disco. El pipeline le pasa resultados por tema y esto
devuelve el snapshot público. Así los umbrales se pueden testear
offline (analysis/test_contract.py).

Si cambias una forma o un umbral, sube CONTRACT_VERSION y docs/CONTRACT.md.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata

CONTRACT_VERSION = 1

# §1. Los 10 países v1. El snapshot los lleva siempre, aunque sea en gris.
V1_COUNTRIES = ("US", "GB", "ES", "MX", "AR", "CL", "FR", "DE", "IT", "BR")

# §3. Umbrales, calibrados a every_n=4 y ventana de 24 h.
TOPIC_MIN_N = 80
TOPIC_MIN_NE = 50
TOPIC_MIN_DOMAINS = 8
TOPIC_MAX_HHI = 0.25        # estricto: hhi < 0.25
TOPIC_MAX_TOP_SHARE = 0.40  # estricto: top_share < 0.40

COUNTRY_MIN_N = 100
COUNTRY_MIN_NE = 70

MAX_TOPICS = 5
MAX_LABEL = 48
DOM_RATIO = 1.2


def normalize_label(label: str) -> str:
    """Clave estable de un tema: minúsculas, sin acentos ni puntuación."""
    s = unicodedata.normalize("NFKD", label or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return " ".join(s.split())


def short_label(label: str) -> str:
    label = " ".join((label or "").split())
    if len(label) <= MAX_LABEL:
        return label
    return label[:MAX_LABEL - 1].rstrip() + "…"


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def topic_is_trust(t: dict) -> bool:
    """
    §3 Tema. `t` es la salida de analyze_topic con status "ok":
    n y ne son on-topic, y la concentración se mide sobre esos mismos
    titulares, no sobre las filas GKG crudas.
    """
    if t.get("status") != "ok":
        return False
    m = t.get("metrics", {})
    sq = t.get("source_quality", {})
    return bool(
        t.get("n_classified", 0) >= TOPIC_MIN_N
        and t.get("n_effective", 0) >= TOPIC_MIN_NE
        and sq.get("n_domains", 0) >= TOPIC_MIN_DOMAINS
        and _finite(sq.get("hhi")) and sq["hhi"] < TOPIC_MAX_HHI
        and _finite(sq.get("top_share"))
        and sq["top_share"] < TOPIC_MAX_TOP_SHARE
        and _finite(m.get("ideological"))
        and _finite(m.get("affective_index"))
        and _finite(m.get("composite"))
    )


def _r3(x: float) -> float:
    return round(float(x), 3)


def build_topic(t: dict, trust: bool) -> dict:
    m = t.get("metrics", {})
    return {
        "l": short_label(t["axis"]["label"]),
        "a": [t["axis"]["pole_a"], t["axis"]["pole_b"]],
        "n": int(t.get("n_classified", 0)),
        "ne": int(math.floor(t.get("n_effective", 0))),
        "ideo": _r3(m["ideological"]) if trust else None,
        "aff": _r3(m["affective_index"]) if trust else None,
        "comp": _r3(m["composite"]) if trust else None,
        "trust": trust,
    }


def _dominant(ideo: float, aff: float) -> str:
    if ideo > aff * DOM_RATIO:
        return "ideo"
    if aff > ideo * DOM_RATIO:
        return "aff"
    return "mix"


def build_country(topic_results: list[dict]) -> dict:
    """
    §2 País. Solo entran en el agregado los temas trust; si no hay
    masa nacional suficiente el país va en gris, sin interpolar.
    """
    # Solo se pueden listar temas con eje (necesitan `l` y `a`).
    listable = [t for t in topic_results
                if t.get("axis") and t.get("status") in
                ("ok", "insufficient_after_filtering")]
    scored = [(t, topic_is_trust(t)) for t in listable]
    topics = [build_topic(t, trust) for t, trust in scored]

    # Fiables primero (por comp desc), luego el resto (por n desc).
    topics.sort(key=lambda x: (not x["trust"],
                               -(x["comp"] or 0), -x["n"]))
    topics = topics[:MAX_TOPICS]

    trusted = [t for t, trust in scored if trust]
    n = sum(int(t["n_classified"]) for t in trusted)
    ne = sum(int(math.floor(t["n_effective"])) for t in trusted)

    country_trust = bool(trusted) and n >= COUNTRY_MIN_N \
        and ne >= COUNTRY_MIN_NE

    if not country_trust:
        return {"trust": False, "color": None, "dom": None,
                "n": n, "ne": ne, "topics": topics}

    w = [t["n_classified"] for t in trusted]
    total = sum(w)

    def wavg(key):
        return sum(t["metrics"][key] * wi
                   for t, wi in zip(trusted, w)) / total

    ideo = wavg("ideological")
    aff = wavg("affective_index")
    comp = wavg("composite")
    return {
        "trust": True,
        "color": _r3(comp),
        "dom": _dominant(ideo, aff),
        "n": n,
        "ne": ne,
        "topics": topics,
    }


def build_snapshot(results_by_country: dict[str, list[dict]],
                   ts: str, window_hours: int) -> dict:
    """Siempre los 10 países v1; un país sin resultados va en gris."""
    return {
        "v": CONTRACT_VERSION,
        "ts": ts,
        "w": int(window_hours),
        "countries": {
            c: build_country(results_by_country.get(c, []))
            for c in V1_COUNTRIES
        },
    }


def dumps(snapshot: dict) -> str:
    return json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def freeze_axis(axis: dict, registry: dict[str, list[str]]) -> dict:
    """
    Orientación congelada por `l` normalizado: si el tema ya salió en
    otra corrida, se reutilizan sus polos (y su orden) para clasificar.
    Si es nuevo, se registra tal cual. Muta `registry`.
    """
    key = normalize_label(axis["label"])
    if key in registry:
        pole_a, pole_b = registry[key]
        return {**axis, "pole_a": pole_a, "pole_b": pole_b}
    registry[key] = [axis["pole_a"], axis["pole_b"]]
    return axis
