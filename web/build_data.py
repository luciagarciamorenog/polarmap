"""
Datos de la web de PolarMap (sin llamadas a la API).

Lee cada ejecución guardada en data/internal/ (<run>_full.json y
<run>_headlines.jsonl), resume por debate y por medio, y escribe
web/index.html a partir de web/template.html con los datos dentro,
de modo que la página funcione sola (abrir con doble clic o subir a
Netlify).

Uso:
    python3 web/build_data.py
    python3 web/build_data.py --runs snapshot_ES_20261120 snapshot_measure
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.outlets import debate_outlets  # noqa: E402

INTERNAL = ROOT / "data" / "internal"
WEB = ROOT / "web"

# Webs de partidos o instituciones que GDELT mezcla con la prensa:
# no son medios, así que no salen en la comparación de medios.
NOT_PRESS = {"pp.es", "psoe.es", "voxespana.es", "podemos.info",
             "movimientosumar.es", "lamoncloa.gob.es"}

NAMES = {
    "abc.es": "ABC", "canalsur.es": "Canal Sur", "canarias7.es": "Canarias7",
    "cope.es": "COPE", "elconfidencial.com": "El Confidencial",
    "eldiario.es": "elDiario.es", "elespanol.com": "El Español",
    "elpais.com": "El País", "europapress.es": "Europa Press",
    "infolibre.es": "infoLibre", "larazon.es": "La Razón",
    "lavanguardia.com": "La Vanguardia", "libertaddigital.com": "Libertad Digital",
    "ondacero.es": "Onda Cero", "publico.es": "Público", "vozpopuli.com": "Vozpópuli",
    "elmundo.es": "El Mundo", "rtve.es": "RTVE", "20minutos.es": "20minutos",
    "okdiario.com": "OKDIARIO", "eldebate.com": "El Debate", "theobjective.com": "The Objective",
    "elcomercio.es": "El Comercio", "diariosur.es": "Diario Sur", "ideal.es": "Ideal",
    "laverdad.es": "La Verdad", "hoy.es": "Hoy", "lasprovincias.es": "Las Provincias",
    "elnortedecastilla.es": "El Norte de Castilla", "eldiariomontanes.es": "El Diario Montañés",
    "diariodesevilla.es": "Diario de Sevilla", "diariodecadiz.es": "Diario de Cádiz",
    "malagahoy.es": "Málaga Hoy", "huelvainformacion.es": "Huelva Información",
    "eldiadecordoba.es": "El Día de Córdoba", "europasur.es": "Europa Sur",
    "diariodenavarra.es": "Diario de Navarra", "diariodeleon.es": "Diario de León",
    "diariodeburgos.es": "Diario de Burgos", "diariodeavila.es": "Diario de Ávila",
    "diariopalentino.es": "Diario Palentino", "diariojaen.es": "Diario Jaén",
    "laregion.es": "La Región", "lavozdigital.es": "La Voz de Cádiz",
    "latribunadetoledo.es": "La Tribuna de Toledo", "latribunadealbacete.es": "La Tribuna de Albacete",
    "lagacetadesalamanca.es": "La Gaceta de Salamanca", "madridiario.es": "Madridiario",
    "economiadigital.es": "Economía Digital", "tercerainformacion.es": "Tercera Información",
    "elpueblodeceuta.es": "El Pueblo de Ceuta", "que.es": "Qué!", "thelocal.es": "The Local",
    "lavozdegalicia.es": "La Voz de Galicia", "ara.cat": "Ara", "naiz.eus": "Naiz",
    "lasexta.com": "laSexta", "antena3.com": "Antena 3", "elcorreo.com": "El Correo",
    "elplural.com": "El Plural", "esdiario.com": "ESdiario", "periodistadigital.com": "Periodista Digital",
    "expansion.com": "Expansión", "estrelladigital.es": "Estrella Digital", "leonoticias.com": "leonoticias",
    "larioja.com": "La Rioja", "vilaweb.cat": "VilaWeb", "andaluciainformacion.es": "Andalucía Información",
    "elprogreso.es": "El Progreso", "nuevatribuna.es": "Nueva Tribuna", "lavozdeasturias.es": "La Voz de Asturias",
    "elconfidencialdigital.com": "El Confidencial Digital", "diariodepontevedra.es": "Diario de Pontevedra",
    "diariovasco.com": "Diario Vasco", "elperiodico.cat": "El Periódico", "infoclm.es": "infoCLM",
    "diaripublic.cat": "Diari Públic", "praza.gal": "Praza", "canarias7.es": "Canarias7",
    "diariodemallorca.es": "Diario de Mallorca", "levante-emv.com": "Levante-EMV",
    "farodevigo.es": "Faro de Vigo", "lne.es": "La Nueva España", "heraldo.es": "Heraldo de Aragón",
}

# La web es una foto del último día y la evolución diaria desde aquí.
# Las corridas anteriores (sep y 20-nov) usaban otra versión del pipeline.
SINCE = "2026-11-24"

HOSTILE = 0.5      # a partir de aquí, el titular ataca o desprecia (codebook: 2)
SIDE = 0.5         # |postura| desde aquí, el titular se inclina claramente a un lado
MONTHS = "ene feb mar abr may jun jul ago sep oct nov dic".split()


def name(domain: str) -> str:
    return NAMES.get(domain, domain)


def date_label(ts: str) -> tuple[str, str]:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d.strftime("%Y-%m-%d"), f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def examples(rows: list[dict], key, n: int = 3) -> list[dict]:
    """Titulares de medios distintos, los más marcados primero."""
    out, seen = [], set()
    for r in sorted(rows, key=key):
        if r["domain"] in seen or not r.get("title"):
            continue
        seen.add(r["domain"])
        out.append({"t": r["title"], "o": name(r["domain"]), "u": r["url"]})
        if len(out) == n:
            break
    return out


def build_run(stem: str) -> dict | None:
    full_p, head_p = INTERNAL / f"{stem}_full.json", INTERNAL / f"{stem}_headlines.jsonl"
    if not (full_p.exists() and head_p.exists()):
        return None
    full = json.loads(full_p.read_text())
    rows = [json.loads(l) for l in head_p.read_text().splitlines() if l.strip()]
    rid, label = date_label(full["ts"])
    country = "ES"
    run = {"id": rid, "label": label, "stem": stem, "country": country,
           "n_headlines": len(rows),
           "none_share": round(sum(1 for r in rows if not r.get("topic")) / max(len(rows), 1), 3),
           "debates": []}
    usage_p = INTERNAL / f"{stem}_usage.json"
    if usage_p.exists():
        run["usd"] = round(json.loads(usage_p.read_text()).get("usd_total", 0), 2)

    scored = [r for r in rows if r.get("topic") and not r.get("off_topic")
              and isinstance(r.get("stance"), (int, float))]
    for t in full["raw_topic_results"].get(country, []):
        ax = t.get("axis") or {}
        topic = ax.get("label")
        m = t.get("metrics") or {}
        sel = [r for r in scored if r["topic"] == topic]
        press = [r for r in sel if r["domain"] not in NOT_PRESS]
        hs = [r.get("hostility") or 0.0 for r in sel]
        deb = {
            "label": topic, "a": ax.get("pole_a"), "b": ax.get("pole_b"),
            "includes": ax.get("includes", [])[:6],
            "n_articles": t.get("n_articles", 0), "n": len(sel),
            "trust": bool(t.get("trustworthy")),
            "ideo": round(m["ideological"], 3) if m.get("ideological") is not None else None,
            "aff": round(m["affective_index"], 3) if m.get("affective_index") is not None else None,
            "hostile_share": round(sum(h >= HOSTILE for h in hs) / len(hs), 3) if hs else None,
            "neutral_share": round(sum(h == 0 for h in hs) / len(hs), 3) if hs else None,
            "us_them": round((t.get("descriptives") or {}).get("us_them_share", 0), 3),
            "outlets": None, "examples": {}}
        if len(press) >= 15:
            d = debate_outlets(press, topic, min_n=3)
            host, side = defaultdict(list), defaultdict(list)
            for r in press:
                host[r["domain"]].append(r.get("hostility") or 0.0)
                side[r["domain"]].append((r["stance"] >= SIDE) - (r["stance"] <= -SIDE))
            deb["outlets"] = {
                "eta2": round(d.eta2, 3), "eta2_null": round(d.eta2_null, 3),
                "p": round(d.p_value, 4),
                "list": [{"o": name(o.domain), "d": o.domain, "n": o.n,
                          "m": round(o.mean, 3), "lo": round(o.lo, 3), "hi": round(o.hi, 3),
                          "h": round(sum(host[o.domain]) / len(host[o.domain]), 3),
                          "hs": round(sum(x >= HOSTILE for x in host[o.domain]) / len(host[o.domain]), 3),
                          # inclinación neta: de cada titular, cuántos más caen de un lado que del otro
                          "nl": round(abs(sum(side[o.domain])) / len(side[o.domain]), 3)}
                         for o in d.positions]}
        if sel:
            conf = lambda r: r.get("confidence") or 0
            # Un titular suelto se clasifica con mucho ruido: como ejemplo de
            # cada lado solo se usan titulares cuyo medio, en conjunto,
            # también se sitúa en ese lado.
            pos = {o["d"]: o["m"] for o in (deb["outlets"] or {}).get("list", [])}
            side = lambda r, s: pos.get(r["domain"], 0) * s > 0.15 if pos else True
            deb["examples"] = {
                "a": examples([r for r in press if r["stance"] <= -0.5 and side(r, -1)],
                              lambda r: (r["stance"], -conf(r))),
                "b": examples([r for r in press if r["stance"] >= 0.5 and side(r, 1)],
                              lambda r: (-r["stance"], -conf(r))),
                "hot": examples([r for r in press if (r.get("hostility") or 0) >= 0.6],
                                lambda r: -(r.get("hostility") or 0)),
            }
        cg = t.get("common_ground") or {}
        if cg.get("status") == "ok" and deb["trust"]:
            ev = {i: {"t": e["title"], "o": name(e["domain"]), "u": e["url"], "s": e["side"]}
                  for i, e in cg["evidence"].items()}
            pick = lambda ids: [dict(ev[i], id=i) for i in ids if i in ev]
            sh, df = cg.get("shared"), cg["differ"]
            deb["cg"] = {
                "n": cg["n_a"] + cg["n_b"],
                "shared": {"es": sh["es"], "en": sh["en"], "ev": pick(sh["ids"])} if sh else None,
                "differ": {"es": df["es"], "en": df["en"], "type": df["type"], "ev": pick(df["ids"])},
            }
        run["debates"].append(deb)
    run["debates"].sort(key=lambda d: (not d["trust"], -d["n"]))
    add_english(run)
    return run


I18N_P = WEB / "i18n_en.json"
I18N = json.loads(I18N_P.read_text()) if I18N_P.exists() else {"labels": {}, "poles": {}, "families": {}}


def add_english(run: dict) -> None:
    """Etiquetas en inglés para la web (los debates los genera el modelo en español)."""
    for d in run["debates"]:
        en, fam = I18N["labels"].get(d["label"], [d["label"], None])
        d["label_en"], d["family"] = en, fam
        d["a_en"] = I18N["poles"].get(d["a"], d["a"])
        d["b_en"] = I18N["poles"].get(d["b"], d["b"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--runs", nargs="*", default=None,
                    help="nombres de ejecución (sin _full.json); por defecto, todas")
    ap.add_argument("--since", default=SINCE,
                    help=f"solo corridas desde esta fecha (AAAA-MM-DD); por defecto {SINCE}, "
                         "inicio de las corridas diarias")
    args = ap.parse_args()
    stems = args.runs or sorted(p.name[:-len("_full.json")] for p in INTERNAL.glob("*_full.json")
                                if "partial" not in p.name and "prueba" not in p.name)
    runs = [r for r in (build_run(s) for s in stems) if r and r["id"] >= args.since]
    runs.sort(key=lambda r: r["id"], reverse=True)
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "families": I18N.get("families", {}), "runs": runs}
    (WEB / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tpl = (WEB / "template.html").read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (WEB / "index.html").write_text(tpl.replace("/*__DATA__*/null", blob))
    for r in runs:
        print(f"{r['label']}: {r['n_headlines']} titulares, {len(r['debates'])} debates "
              f"({sum(d['trust'] for d in r['debates'])} fiables)")
    print(f"→ {WEB / 'index.html'}")


if __name__ == "__main__":
    main()
