"""
Convierte las tablas CSV de Gemini (data/gold/gemini_<país>_<fecha>/tanda*.csv)
al formato de etiquetas de score_gold.py: un JSON {id: etiqueta}.

Las etiquetas de Gemini son un anotador aparte («gemini»): NO son el set de
referencia humano y no se mezclan con las de la página de etiquetado.

Uso:
    python3 validate/import_gemini.py data/gold/gemini_ES_20261125 \
        [--base data/gold/labels_gemini_tanda1_ES_20261125.json]

Orden: --base (etiquetas ya en JSON, p. ej. la tanda 1 hecha en la página),
después tanda*.csv y por último correcciones*.csv (revisiones de Gemini, en
orden alfabético), que sustituyen la fila entera de ese id. Con --base la
salida es labels_gemini_todas_<país>_<fecha>.json.
"""
import csv, json, sys
from pathlib import Path

TOPIC = {f"T{i}": i - 1 for i in range(1, 11)}
NONE_TYPE = {"sin_postura": "no_stance", "politico_otro": "political",
             "controversia_no_politica": "nonpolitical_controversy", "otro": "other", "": None}
US = {"no": "no", "politicos": "political", "grupos_sociales": "social_groups",
      "rivales_no_politicos": "nonpolitical_rivals"}

def main(folder: str, base: list[str] = ()):
    folder = Path(folder)
    out, problems = {}, []
    for b in base:
        out.update(json.loads(Path(b).read_text()))
    # Las tandas primero; después correcciones.csv (revisión de Gemini sobre
    # sus propias respuestas), que sustituye la fila entera de ese id.
    files = sorted(folder.glob("tanda*.csv")) + sorted(folder.glob("correcciones*.csv"))
    for f in files:
        revision = f.name.startswith("correcciones")
        for row in csv.DictReader(f.open(encoding="utf-8"), delimiter=";"):
            rid = row["id"].strip()
            tema = row["tema"].strip()
            post = row["postura"].strip()
            l = {"item": rid, "labeled_by": "gemini", "input": "headline_lead"}
            if tema == "ninguno":
                l["topic"] = "none"; l["stance"] = None
                l["none_type"] = NONE_TYPE.get(row["tipo"].strip(), "??")
            elif tema in TOPIC:
                l["topic"] = TOPIC[tema]; l["none_type"] = None
                l["stance"] = None if post == "" else ("na" if post == "na" else float(post))
                if TOPIC[tema] < 5 and post == "":
                    problems.append(f"{rid}: tema {tema} sin postura")
            else:
                problems.append(f"{rid}: tema desconocido {tema!r}")
            h = row["hostilidad"].strip()
            l["hostility"] = int(h) if h in ("0", "1", "2") else None
            if l["hostility"] is None: problems.append(f"{rid}: hostilidad {h!r}")
            u = row["nosotros_ellos"].strip()
            l["us_them"] = US.get(u)
            if l["us_them"] is None: problems.append(f"{rid}: nosotros_ellos {u!r}")
            if l.get("none_type") == "??": problems.append(f"{rid}: tipo {row['tipo']!r}")
            l["unsure"] = row["dudoso"].strip() == "si"
            if (row.get("nota") or "").strip(): l["note"] = row["nota"].strip()
            if revision:
                if rid not in out: problems.append(f"{rid}: corrección sin fila original")
                prev = out.get(rid, {}).get("revised")
                l["revised"] = (prev if isinstance(prev, list) else []) + [f.stem]
            elif rid in out:
                problems.append(f"{rid}: repetido")
            out[rid] = l
    name = folder.name.replace("gemini_", "labels_gemini_todas_" if base else "labels_gemini_")
    dest = folder.parent / (name + ".json")
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"{len(out)} etiquetas de Gemini → {dest}")
    for p in problems: print("  PROBLEMA", p)
    return out

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--base", action="append", default=[])
    a = ap.parse_args()
    main(a.folder, a.base)
