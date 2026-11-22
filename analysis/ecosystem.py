"""
Polarización del ecosistema de medios: los periódicos como unidad.

Por qué existe
--------------
El índice por titular (metrics.esteban_ray sobre cada noticia) responde a
«¿cuánto opina la prensa?». Pero un periódico publica teletipos, citas de
ambos bandos y datos sin ángulo: el medio explica solo ~30 % de la postura
de sus titulares. Este módulo responde a otra pregunta, que es la que
importa para hablar de polarización mediática:

    ¿se separan los periódicos en dos bandos, y cuánto?

Y añade las piezas que el índice por titular no ve:

  1. Índice por titular con un medio, un voto: cada periódico pesa lo mismo,
     publique 5 titulares o 200 (hoy pesan más La Razón o Europa Press).
  2. Bandos de periódicos: la mejor partición en dos de las posiciones
     medias de los medios, la distancia entre ambos bandos (en la escala
     −1…+1) y cuánta saldría por azar barajando las posturas entre medios.
     Lo que supera al azar es separación editorial. Más la prueba de 1, 2
     o 3 grupos (BIC).
  3. Afectiva por medio: ¿los periódicos más extremos son más hostiles?
  4. Opinión frente a noticia: columnas y editoriales (por la URL) aparte.
  5. Eje Gobierno ↔ oposición (campo `bloc`, si la corrida lo tiene): las
     posiciones de los medios en un eje fijo, comparable entre días.
  6. Polarización de agenda: qué debates elige cubrir cada medio. Dos
     periódicos pueden no discrepar en ningún titular y aun así contar
     historias distintas (desahucios frente a okupación).

Entrada: los registros por titular que escribe cada corrida en
data/internal/<run>_headlines.jsonl. Sin llamadas a la API.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from analysis.metrics import esteban_ray

MIN_PER_OUTLET = 3          # titulares de un medio en un debate para situarlo
MIN_AGENDA = 10             # titulares en debates para medir la agenda de un medio

# Tramos de la URL que marcan opinión (columnas, editoriales, tribunas).
OPINION_SEGMENTS = {
    "opinion", "opinión", "opinio", "iritzia", "tribuna", "tribunas",
    "editorial", "editoriales", "columnas", "columnistas", "columna",
    "firmas", "firma", "blogs", "blog", "cartas", "cartas-al-director",
    "voces", "el-blog-de", "ideas",
}


# ─────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────

def genre_from_url(url: str) -> str:
    """'opinion' si algún tramo de la ruta es de opinión; si no, 'news'."""
    try:
        parts = [p.lower() for p in urlparse(url).path.split("/") if p]
    except ValueError:
        return "news"
    return "opinion" if any(p in OPINION_SEGMENTS for p in parts[:-1]) else "news"


def load_records(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines()
            if line.strip()]


def scored(rows: list[dict], field: str = "stance") -> list[dict]:
    """Titulares de un debate, en tema y con el campo numérico pedido."""
    return [r for r in rows
            if r.get("topic") and r.get("topic") != "__failed__"
            and not r.get("off_topic") and not r.get("stance_failed")
            and isinstance(r.get(field), (int, float))
            and np.isfinite(r.get(field))]


def _eta2(groups: np.ndarray, values: np.ndarray) -> float:
    grand = values.mean()
    sst = float(((values - grand) ** 2).sum())
    if sst == 0:
        return 0.0
    return sum(float((groups == g).sum()) * (values[groups == g].mean() - grand) ** 2
               for g in np.unique(groups)) / sst


def _outlet_means(groups: np.ndarray, values: np.ndarray) -> dict[str, float]:
    return {g: float(values[groups == g].mean()) for g in np.unique(groups)}


def best_two_split(x: np.ndarray) -> tuple[float, int]:
    """
    Mejor partición de valores 1D en dos bandos (la que maximiza la
    varianza entre bandos, como Otsu/Jenks). Devuelve (distancia entre
    las medias de los dos bandos, tamaño del bando izquierdo).
    """
    x = np.sort(np.asarray(x, dtype=float))
    n = len(x)
    if n < 2:
        return 0.0, n
    best = (-1.0, 0.0, 1)
    for i in range(1, n):
        a, b = x[:i], x[i:]
        between = i * (n - i) * (b.mean() - a.mean()) ** 2
        if between > best[0]:
            best = (between, float(b.mean() - a.mean()), i)
    return best[1], best[2]


def _groups_k(positions: np.ndarray) -> dict:
    """¿1, 2 o 3 grupos de medios? (mezcla gaussiana + BIC, del piloto)."""
    try:
        from validate.pilot.stats import detect_groups, standardize
    except ImportError:                       # pragma: no cover
        return {"k": None}
    if len(positions) < 10 or positions.std() == 0:
        return {"k": None, "note": "menos de 10 medios"}
    g = detect_groups(standardize(positions), kmax=3, min_share=0.15)
    return {"k": int(g.k), "sizes": [round(float(s), 2) for s in g.sizes],
            "bic": {int(k): round(float(v), 1) for k, v in g.bic.items()}}


# ─────────────────────────────────────────────────────────────
# 1-3. Un debate (o un eje): medios, bandos, afectiva
# ─────────────────────────────────────────────────────────────

def axis_report(rows: list[dict], field: str = "stance",
                min_n: int = MIN_PER_OUTLET,
                n_perm: int = 2000, seed: int = 0) -> dict:
    """
    rows: titulares de un mismo eje con `domain` y el campo numérico.
    Devuelve índices por titular y por medio, bandos de medios y afectiva.
    """
    rng = np.random.default_rng(seed)
    all_vals = np.array([r[field] for r in rows], dtype=float)
    out: dict = {"n_headlines": len(rows),
                 "er_headline": _r(esteban_ray(all_vals)) if len(rows) >= 10 else None,
                 "center_share": _r(float(np.mean(np.abs(all_vals) < 0.15))) if len(rows) else None}

    counts = Counter(r["domain"] for r in rows)
    keep = [r for r in rows if counts[r["domain"]] >= min_n]
    if len({r["domain"] for r in keep}) < 4:
        out["note"] = "menos de 4 medios con datos suficientes"
        out["n_outlets"] = len({r["domain"] for r in keep})
        return out

    groups = np.array([r["domain"] for r in keep])
    vals = np.array([r[field] for r in keep], dtype=float)
    c = Counter(groups)
    weights = np.array([1.0 / c[g] for g in groups])

    # 1. Un medio, un voto
    out["er_outlet_weighted"] = _r(esteban_ray(vals, weights=weights))

    # Efecto medio (cuánto explica el periódico) y prueba de permutación
    eta = _eta2(groups, vals)
    null_eta = np.array([_eta2(groups, rng.permutation(vals)) for _ in range(n_perm)])
    out["outlet_eta2"] = _r(eta)
    out["outlet_eta2_chance"] = _r(float(null_eta.mean()))
    out["outlet_p"] = _r(float((np.sum(null_eta >= eta) + 1) / (n_perm + 1)), 4)

    # 2. Bandos de medios
    means = _outlet_means(groups, vals)
    pos = np.array(list(means.values()))
    gap, n_left = best_two_split(pos)
    order = sorted(means, key=means.get)
    left, right = order[:n_left], order[n_left:]

    # Sin intervalo bootstrap a propósito: remuestrear titulares añade ruido
    # a cada media y siempre agranda la distancia, así que el intervalo sale
    # desplazado. La referencia honesta es el azar: cuánta distancia
    # producirían las mismas posturas repartidas al azar entre los mismos
    # medios. Lo que supera eso es separación editorial.
    idx = {g: np.where(groups == g)[0] for g in means}
    # Azar: barajar posturas entre medios (mismos tamaños)
    null_gap = []
    for _ in range(n_perm):
        pv = rng.permutation(vals)
        null_gap.append(best_two_split(np.array(
            [pv[ix].mean() for ix in idx.values()]))[0])
    null_gap = np.array(null_gap)

    out["n_outlets"] = len(means)
    out["few_outlets"] = len(means) < 8          # orientativo, no concluyente
    out["camps"] = {
        "distance": _r(gap),                                   # en la escala −1…+1
"distance_chance": _r(float(null_gap.mean())),
        "excess_over_chance": _r(gap - float(null_gap.mean())),
        "p": _r(float((np.sum(null_gap >= gap) + 1) / (n_perm + 1)), 4),
        "left": [(d, _r(means[d]), c[d]) for d in left],
        "right": [(d, _r(means[d]), c[d]) for d in right],
        "groups": _groups_k(pos),
    }

    # 3. Afectiva por medio: ¿los extremos son más hostiles?
    if field == "stance" and all(isinstance(r.get("hostility"), (int, float)) for r in keep):
        hos = np.array([r["hostility"] for r in keep], dtype=float)
        h_out = _outlet_means(groups, hos)
        ext = np.array([abs(means[d] - np.mean(pos)) for d in means])
        hh = np.array([h_out[d] for d in means])
        rho = _spearman(ext, hh)
        out["affective"] = {
            "mean_hostility": _r(float(hos.mean())),
            "rho_extremity_hostility": _r(rho),
            "most_hostile": sorted(((d, _r(h)) for d, h in h_out.items()),
                                   key=lambda t: -t[1])[:3],
        }
    return out


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 4 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def _r(x, nd: int = 3):
    try:
        return None if x is None or not np.isfinite(x) else round(float(x), nd)
    except TypeError:
        return x


# ─────────────────────────────────────────────────────────────
# 4. Opinión frente a noticia
# ─────────────────────────────────────────────────────────────

def genre_report(rows: list[dict]) -> dict:
    out = {}
    for genre in ("news", "opinion"):
        sel = [r for r in rows if genre_from_url(r.get("url", "")) == genre]
        v = np.array([r["stance"] for r in sel], dtype=float)
        out[genre] = {
            "n": len(sel),
            "mean_abs_stance": _r(float(np.abs(v).mean())) if len(v) else None,
            "er_headline": _r(esteban_ray(v)) if len(v) >= 10 else None,
            "mean_hostility": _r(float(np.mean([r.get("hostility", np.nan) for r in sel])))
            if sel else None,
        }
    return out


# ─────────────────────────────────────────────────────────────
# 6. Polarización de agenda
# ─────────────────────────────────────────────────────────────

def _js(p: np.ndarray, q: np.ndarray) -> float:
    """Distancia de Jensen-Shannon (base 2, en [0, 1])."""
    m = (p + q) / 2

    def kl(a, b):
        mask = a > 0
        return float(np.sum(a[mask] * np.log2(a[mask] / b[mask])))
    return float(np.sqrt(max(0.0, (kl(p, m) + kl(q, m)) / 2)))


def agenda_report(records: list[dict], min_n: int = MIN_AGENDA,
                  n_perm: int = 1000, seed: int = 0) -> dict:
    """
    Reparto de los titulares de cada medio entre los debates del día.
    Distancia media entre las agendas de los medios frente a la que saldría
    si cada titular fuera a parar a un medio al azar.
    """
    rng = np.random.default_rng(seed)
    rows = [r for r in records if r.get("topic") and r["topic"] != "__failed__"]
    topics = sorted({r["topic"] for r in rows})
    counts = Counter(r["domain"] for r in rows)
    rows = [r for r in rows if counts[r["domain"]] >= min_n]
    outlets = sorted({r["domain"] for r in rows})
    if len(outlets) < 4 or len(topics) < 2:
        return {"note": "pocos medios o debates para medir agenda",
                "n_outlets": len(outlets), "n_debates": len(topics)}
    t_index = {t: i for i, t in enumerate(topics)}
    dom = np.array([r["domain"] for r in rows])
    top = np.array([t_index[r["topic"]] for r in rows])

    def mean_js(d, t):
        dist = []
        for o in outlets:
            h = np.bincount(t[d == o], minlength=len(topics)).astype(float)
            dist.append(h / h.sum())
        return float(np.mean([_js(dist[i], dist[j])
                              for i in range(len(dist)) for j in range(i + 1, len(dist))])), dist

    obs, dists = mean_js(dom, top)
    null = np.array([mean_js(dom, rng.permutation(top))[0] for _ in range(n_perm)])
    overall = np.bincount(top, minlength=len(topics)) / len(top)
    lifts = {}
    for o, dist in zip(outlets, dists):
        i = int(np.argmax(dist / np.maximum(overall, 1e-9)))
        lifts[o] = (topics[i], _r(float(dist[i] / overall[i]), 2), int(counts[o]))
    return {
        "n_outlets": len(outlets), "n_debates": len(topics),
        "mean_js": _r(obs), "mean_js_chance": _r(float(null.mean())),
        "excess_over_chance": _r(obs - float(null.mean())),
        "p": _r(float((np.sum(null >= obs) + 1) / (n_perm + 1)), 4),
        "share_by_debate": {t: _r(float(s)) for t, s in zip(topics, overall)},
        "most_overrepresented": lifts,
    }


# ─────────────────────────────────────────────────────────────
# Informe completo de una corrida
# ─────────────────────────────────────────────────────────────

def ecosystem_report(records: list[dict], country: str | None = None,
                     seed: int = 0) -> dict:
    if country:
        records = [r for r in records if r.get("country") == country]
    rows = scored(records, "stance")
    debates = [t for t, _ in Counter(r["topic"] for r in rows).most_common()]
    rep: dict = {"n_headlines": len(records), "n_scored": len(rows), "debates": {}}
    for t in debates:
        sel = [r for r in rows if r["topic"] == t]
        rep["debates"][t] = axis_report(sel, "stance", seed=seed)
        rep["debates"][t]["genre"] = genre_report(sel)

    bloc_rows = scored(records, "bloc")
    rep["bloc"] = (axis_report(bloc_rows, "bloc", seed=seed)
                   if len(bloc_rows) >= 30 else
                   {"note": "la corrida no tiene el eje Gobierno ↔ oposición"
                            if not bloc_rows else "menos de 30 titulares con eje Gobierno ↔ oposición",
                    "n_headlines": len(bloc_rows)})
    rep["agenda"] = agenda_report(records, seed=seed)
    g = Counter(genre_from_url(r.get("url", "")) for r in records)
    rep["opinion_share"] = _r(g["opinion"] / max(1, sum(g.values())))
    return rep


def summary_lines(rep: dict) -> list[str]:
    """Resumen legible del informe, para el log y la consola."""
    L = [f"{rep['n_scored']} titulares puntuados de {rep['n_headlines']} · "
         f"opinión {rep['opinion_share']:.0%} del total"]
    for t, d in rep["debates"].items():
        flag = "  · pocos medios: orientativo" if d.get("few_outlets") else ""
        L.append(f"\n{t}  ({d['n_headlines']} titulares, {d.get('n_outlets', 0)} medios){flag}")
        L.append(f"  índice por titular {d['er_headline']} · un medio, un voto "
                 f"{d.get('er_outlet_weighted')}")
        if "camps" not in d:
            L.append(f"  {d.get('note', '')}")
            continue
        c = d["camps"]
        L.append(f"  el medio explica {d['outlet_eta2']:.0%} de la postura "
                 f"(azar {d['outlet_eta2_chance']:.0%}, p={d['outlet_p']})")
        L.append(f"  distancia entre bandos de medios {c['distance']} · por azar "
                 f"{c['distance_chance']} · exceso {c['excess_over_chance']} · p={c['p']} "
                 f"· grupos (BIC): {c['groups'].get('k')}")
        L.append("  bando A: " + ", ".join(f"{x[0]} {x[1]:+.2f}" for x in c["left"]))
        L.append("  bando B: " + ", ".join(f"{x[0]} {x[1]:+.2f}" for x in c["right"]))
        if "affective" in d:
            a = d["affective"]
            L.append(f"  hostilidad media {a['mean_hostility']} · extremos más hostiles "
                     f"(ρ) {a['rho_extremity_hostility']}")
        gn = d["genre"]
        L.append(f"  noticia n={gn['news']['n']} |postura| {gn['news']['mean_abs_stance']} · "
                 f"opinión n={gn['opinion']['n']} |postura| {gn['opinion']['mean_abs_stance']}")
    b = rep["bloc"]
    L.append("\nEje Gobierno ↔ oposición")
    if "camps" in b:
        L.append(f"  {b['n_headlines']} titulares · medio explica {b['outlet_eta2']:.0%} "
                 f"(p={b['outlet_p']}) · distancia entre bandos {b['camps']['distance']} "
                 f"(azar {b['camps']['distance_chance']})")
    else:
        L.append(f"  {b.get('note')}")
    a = rep["agenda"]
    L.append("\nAgenda")
    if "mean_js" in a:
        L.append(f"  distancia media entre agendas {a['mean_js']} (azar "
                 f"{a['mean_js_chance']}, p={a['p']}) · {a['n_outlets']} medios")
        for o, (t, lift, n) in sorted(a["most_overrepresented"].items(),
                                      key=lambda kv: -kv[1][1])[:6]:
            L.append(f"  {o}: {t} ×{lift}")
    else:
        L.append(f"  {a.get('note')}")
    return L


# ─────────────────────────────────────────────────────────────
# Capa social: prensa frente a redes (pipeline_social.py)
# ─────────────────────────────────────────────────────────────

LAYERS = ("prensa", "politica_oficial", "bluesky_cuentas", "bluesky_respuestas", "youtube_comentarios")


def _layer_stats(rows: list[dict]) -> dict:
    """Medidas comparables entre capas para un conjunto de textos puntuados."""
    if not rows:
        return {"n": 0}
    v = np.array([r["stance"] for r in rows], dtype=float)
    hos = np.array([r.get("hostility", np.nan) for r in rows], dtype=float)
    ut = [r.get("us_them") for r in rows if r.get("us_them")]
    bloc = np.array([r["bloc"] for r in rows if isinstance(r.get("bloc"), (int, float))], dtype=float)
    return {
        "n": len(rows),
        "er": _r(esteban_ray(v)) if len(v) >= 10 else None,
        "mean_abs_stance": _r(float(np.abs(v).mean())),
        "center_share": _r(float(np.mean(np.abs(v) < 0.15))),
        "mean_hostility": _r(float(np.nanmean(hos))) if np.isfinite(hos).any() else None,
        "us_them_share": _r(sum(u != "no" for u in ut) / len(ut)) if ut else None,
        "bloc_n": int(len(bloc)),
        "bloc_mean": _r(float(bloc.mean())) if len(bloc) else None,
        "bloc_pro_gov": _r(float(np.mean(bloc <= -0.5))) if len(bloc) else None,
        "bloc_pro_opp": _r(float(np.mean(bloc >= 0.5))) if len(bloc) else None,
    }


def layers_report(press: list[dict], social: list[dict], accounts: dict | None = None) -> dict:
    """
    Prensa y redes, con los mismos debates y el mismo codebook. Por debate y
    capa: índice, |postura| media, centro, hostilidad, «nosotros/ellos» y
    eje Gobierno ↔ oposición. Más la composición de cada capa (cuánto del
    total trata de algún debate, hacia qué bloque se inclina) y, para las
    cuentas de Bluesky, qué grupos de la lista publicaron de verdad: si
    faltan casi todos los de un lado, la capa no es comparable sin más.
    """
    press = [dict(r, layer="prensa") for r in press]
    allrec = press + social
    debates = [t for t, _ in Counter(r["topic"] for r in scored(press)).most_common()]
    rep: dict = {"debates": {}, "layers": {}}
    for t in debates:
        rep["debates"][t] = {}
        for L in LAYERS:
            sel = [r for r in scored(allrec) if r["layer"] == L and r["topic"] == t]
            st = _layer_stats(sel)
            if L == "bluesky_cuentas" and len({r["source"] for r in sel}) >= 4:
                st["accounts"] = axis_report(
                    [dict(r, domain=r["source"]) for r in sel], "stance", min_n=2, n_perm=500)
            rep["debates"][t][L] = st
    for L in LAYERS:
        rows = [r for r in allrec if r.get("layer") == L]
        if not rows:
            continue
        on = scored(rows)
        rep["layers"][L] = {
            "n_texts": len(rows),
            "share_on_debates": _r(len([r for r in rows if r.get("topic") not in (None, "__failed__")]) / len(rows)),
            **{k: v for k, v in _layer_stats(on).items() if k.startswith("bloc") or k == "mean_hostility"},
        }
    rep["actors"] = actors_report(press, social, accounts or {})
    if accounts:
        posted = Counter()
        for r in social:
            if r.get("layer") == "bluesky_cuentas":
                posted[r["source"]] += 1
        rep["bluesky_coverage"] = {
            g: {"listed": sum(1 for a in accounts.get("bluesky", []) if a["group"] == g and a.get("handle")),
                "posted": len({a["handle"] for a in accounts.get("bluesky", [])
                               if a["group"] == g and posted.get(a.get("handle"))})}
            for g in ("medio", "partido", "politico", "periodista", "gobierno")}
    return rep


def actors_report(press: list[dict], social: list[dict], accounts: dict) -> dict:
    """
    Gobierno y partidos como emisores, igual que un medio: sus notas de
    prensa y sus cuentas oficiales de Bluesky. Para cada actor:
      - cuánto ataca: hostilidad media y «nosotros contra ellos»;
      - dónde se sitúa en cada debate (postura media);
      - qué agenda empuja: qué parte de lo que publica cae en cada debate,
        frente al peso de ese debate en la prensa (>1 = lo empuja más que
        la prensa).
    El eje Gobierno ↔ oposición no se usa aquí: cada actor cae en su lado
    por definición.
    """
    name_of = {a["handle"]: a["name"] for a in accounts.get("bluesky", []) if a.get("handle")}
    rows = []
    for r in social:
        if r.get("layer") == "politica_oficial":
            rows.append(dict(r, actor=r["source"], channel="notas"))
        elif r.get("layer") == "bluesky_cuentas" and r.get("group") in ("gobierno", "partido"):
            rows.append(dict(r, actor=name_of.get(r["source"], r["source"]), channel="bluesky"))
    if not rows:
        return {"note": "sin textos de Gobierno ni partidos en esta corrida"}
    press_scored = scored(press)
    press_debates = [r["topic"] for r in press if r.get("topic") not in (None, "__failed__")]
    press_share = {t: n / max(1, len(press)) for t, n in Counter(press_debates).items()}
    out = {}
    for actor in sorted({r["actor"] for r in rows}):
        sel = [r for r in rows if r["actor"] == actor]
        sc = scored(sel)
        ut = [r.get("us_them") for r in sc if r.get("us_them")]
        per_debate = {}
        for t in sorted({r["topic"] for r in sc}):
            v = np.array([r["stance"] for r in sc if r["topic"] == t], dtype=float)
            per_debate[t] = {"n": len(v), "stance": _r(float(v.mean()))}
        topics = Counter(r.get("topic") for r in sel if r.get("topic") not in (None, "__failed__"))
        out[actor] = {
            "group": sel[0].get("group"),
            "n_texts": len(sel),
            "channels": dict(Counter(r["channel"] for r in sel)),
            "share_on_debates": _r(sum(topics.values()) / len(sel)),
            "mean_hostility": _r(float(np.mean([r["hostility"] for r in sc]))) if sc else None,
            "us_them_share": _r(sum(u != "no" for u in ut) / len(ut)) if ut else None,
            "stance_by_debate": per_debate,
            "agenda_vs_press": {t: _r((n / len(sel)) / press_share[t], 2)
                                for t, n in topics.items() if press_share.get(t)},
        }
    press_h = [r["hostility"] for r in press_scored if isinstance(r.get("hostility"), (int, float))]
    return {"actors": out, "press_mean_hostility": _r(float(np.mean(press_h))) if press_h else None}


def layers_summary_lines(rep: dict) -> list[str]:
    names = {"prensa": "Prensa", "politica_oficial": "Gobierno y partidos", "bluesky_cuentas": "Bluesky cuentas",
             "bluesky_respuestas": "Bluesky respuestas", "youtube_comentarios": "YouTube comentarios"}
    L = ["Composición de cada capa:"]
    for k, v in rep["layers"].items():
        L.append(f"  {names[k]:20s} {v['n_texts']:5d} textos · {v['share_on_debates']:.0%} en debates · "
                 f"bloque: pro-Gobierno {v.get('bloc_pro_gov')} / pro-oposición {v.get('bloc_pro_opp')} "
                 f"· hostilidad {v.get('mean_hostility')}")
    if "bluesky_coverage" in rep:
        L.append("  Cuentas de Bluesky que publicaron: " + ", ".join(
            f"{g} {c['posted']}/{c['listed']}" for g, c in rep["bluesky_coverage"].items()))
    A = rep.get("actors", {})
    if "actors" in A:
        L.append(f"\nGobierno y partidos como emisores (hostilidad media de la prensa: {A['press_mean_hostility']})")
        L.append(f"  {'actor':24s} {'textos':>6} {'hostil.':>8} {'nos/ellos':>9}  agenda frente a la prensa")
        for a, v in sorted(A["actors"].items(), key=lambda kv: -(kv[1]["mean_hostility"] or 0)):
            ag = ", ".join(f"{t[:22]} ×{x}" for t, x in sorted(v["agenda_vs_press"].items(), key=lambda kv: -kv[1])[:2])
            L.append(f"  {a[:24]:24s} {v['n_texts']:>6} {str(v['mean_hostility']):>8} {str(v['us_them_share']):>9}  {ag}")
    for t, layers in rep["debates"].items():
        L.append(f"\n{t}")
        L.append(f"  {'capa':20s} {'n':>5} {'índice':>7} {'|postura|':>9} {'centro':>7} {'hostil.':>8} {'nos/ellos':>9}")
        for k, s in layers.items():
            if not s.get("n"):
                L.append(f"  {names[k]:20s} {0:>5}")
                continue
            L.append(f"  {names[k]:20s} {s['n']:>5} {str(s['er']):>7} {str(s['mean_abs_stance']):>9} "
                     f"{str(s['center_share']):>7} {str(s['mean_hostility']):>8} {str(s['us_them_share']):>9}")
            if "accounts" in s and "camps" in s["accounts"]:
                c = s["accounts"]["camps"]
                L.append(f"  {'':20s} cuentas en dos bandos: distancia {c['distance']} (azar {c['distance_chance']}, p={c['p']})")
    return L
