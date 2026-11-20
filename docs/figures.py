"""
Figures for the README and the portfolio page (no API calls).

    python3 docs/figures.py data/internal/snapshot_measure_headlines.jsonl

Writes to docs/img/ (SVG, plus PNG if rsvg-convert is installed):
  outlets_housing_{en,es}  each outlet's mean stance on the housing debate,
                           with its bootstrap 90 % interval
  pipeline_{en,es}         the six steps of the pipeline
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.outlets import debate_outlets, load_headlines  # noqa: E402

TOPIC = "Crisis de vivienda y desahucios"
MIN_N = 4

NAMES = {
    "publico.es": "Público", "eldiario.es": "elDiario.es",
    "infolibre.es": "infoLibre", "lavanguardia.com": "La Vanguardia",
    "europapress.es": "Europa Press (agency)",
    "elnortedecastilla.es": "El Norte de Castilla", "elpais.com": "El País",
    "abc.es": "ABC", "elconfidencial.com": "El Confidencial",
    "larazon.es": "La Razón", "libertaddigital.com": "Libertad Digital",
}

TEXT = {
    "en": {
        "left": "← The state must step in",
        "right": "More supply, respect contracts →",
        "foot": "Mean stance of each outlet's housing headlines · 25 Sep 2026 · "
                "bar = 90 % bootstrap interval · n = headlines",
        "agency": "Europa Press (agency)",
    },
    "es": {
        "left": "← El Estado debe intervenir",
        "right": "Más oferta y respetar contratos →",
        "foot": "Postura media de los titulares de vivienda de cada medio · "
                "25 sep 2026 · barra = intervalo bootstrap del 90 % · n = titulares",
        "agency": "Europa Press (agencia)",
    },
}

INK, INK2, INK3 = "#0E0C09", "#5A5248", "#968A80"
DOT, BAR, GRID = "#A07038", "#C8A57A", "#DDD8CE"


def svg(lang: str, positions) -> str:
    t = TEXT[lang]
    W, left, right, top, row = 680, 190, 650, 46, 30
    H = top + row * len(positions) + 58
    x = lambda v: left + (v + 1) / 2 * (right - left)  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'role="img" font-family="DM Sans, system-ui, sans-serif">',
           f'<title>{escape(t["foot"])}</title>',
           f'<rect width="{W}" height="{H}" fill="#FAFAF8"/>']
    for v in (-1, -0.5, 0, 0.5, 1):
        out.append(f'<line x1="{x(v):.1f}" y1="{top - 10}" x2="{x(v):.1f}" '
                   f'y2="{H - 50}" stroke="{GRID}" stroke-width="{1 if v == 0 else 0.5}"/>')
        out.append(f'<text x="{x(v):.1f}" y="{H - 34}" font-size="11" '
                   f'fill="{INK3}" text-anchor="middle">{v:+g}</text>'
                   .replace("+0<", "0<"))
    out.append(f'<text x="{left}" y="22" font-size="12" fill="{INK2}">'
               f'{escape(t["left"])}</text>')
    out.append(f'<text x="{right}" y="22" font-size="12" fill="{INK2}" '
               f'text-anchor="end">{escape(t["right"])}</text>')
    for i, o in enumerate(positions):
        y = top + i * row + row / 2
        name = NAMES.get(o.domain, o.domain)
        if o.domain == "europapress.es":
            name = t["agency"]
        tip = f"{name}: {o.mean:+.2f} [{o.lo:+.2f}, {o.hi:+.2f}] · n={o.n}"
        out.append(f'<g><title>{escape(tip)}</title>'
                   f'<rect x="0" y="{y - row / 2:.1f}" width="{W}" height="{row}" '
                   f'fill="transparent"/>'
                   f'<text x="{left - 14}" y="{y + 4:.1f}" font-size="12.5" '
                   f'fill="{INK}" text-anchor="end">{escape(name)} '
                   f'<tspan fill="{INK3}" font-size="11">· {o.n}</tspan></text>'
                   f'<line x1="{x(o.lo):.1f}" y1="{y:.1f}" x2="{x(o.hi):.1f}" '
                   f'y2="{y:.1f}" stroke="{BAR}" stroke-width="2" '
                   f'stroke-linecap="round"/>'
                   f'<circle cx="{x(o.mean):.1f}" cy="{y:.1f}" r="5" fill="{DOT}" '
                   f'stroke="#FAFAF8" stroke-width="2"/></g>')
    out.append(f'<text x="{W / 2}" y="{H - 10}" font-size="10.5" fill="{INK3}" '
               f'text-anchor="middle">{escape(t["foot"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    rows = load_headlines(sys.argv[1] if len(sys.argv) > 1 else
                          ROOT / "data/internal/snapshot_measure_headlines.jsonl")
    d = debate_outlets(rows, TOPIC, min_n=MIN_N)
    img = ROOT / "docs/img"
    img.mkdir(parents=True, exist_ok=True)
    for lang in ("en", "es"):
        path = img / f"outlets_housing_{lang}.svg"
        path.write_text(svg(lang, d.positions))
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-z", "2", str(path), "-o",
                            str(path.with_suffix(".png"))], check=True)
        print("→", path)
    print(f"{TOPIC}: eta²={d.eta2:.2f} (chance {d.eta2_null:.2f}), "
          f"p={d.p_value:.4f}, {d.n_outlets} outlets")


# ─────────────────────────────────────────────────────────────
# Pipeline diagram
# ─────────────────────────────────────────────────────────────

STEPS = {
    "en": [
        ("1 · Collect", ["GDELT news files, every 15 min", "3,200 Spanish headlines / day"], False),
        ("2 · Clean", ["Real headline + lead", "1,728 kept · publisher opt-outs out"], False),
        ("3 · Discover debates", ["Claude Sonnet reads them all", "4 debates, 2 sides each"], True),
        ("4 · Assign", ["Claude Haiku, in batches", "329 on a debate · 95–97 % right"], True),
        ("5 · Score", ["Claude Sonnet, per headline", "side · hostility · framing"], True),
        ("6 · Measure", ["Outlet positions + indices", "shown only if data suffice"], False),
    ],
    "es": [
        ("1 · Recoger", ["Ficheros de GDELT cada 15 min", "3.200 titulares españoles / día"], False),
        ("2 · Limpiar", ["Titular real + entradilla", "1.728 · respeta reservas TDM"], False),
        ("3 · Descubrir debates", ["Claude Sonnet los lee todos", "4 debates, 2 bandos cada uno"], True),
        ("4 · Asignar", ["Claude Haiku, por lotes", "329 en debate · 95–97 % acierto"], True),
        ("5 · Puntuar", ["Claude Sonnet, por titular", "bando · hostilidad · encuadre"], True),
        ("6 · Medir", ["Posición por medio + índices", "solo si hay datos suficientes"], False),
    ],
}
LEGEND = {"en": "Gold = step done by an LLM (Claude) · grey = code and statistics",
          "es": "Dorado = lo hace un LLM (Claude) · gris = código y estadística"}


def pipeline_svg(lang: str) -> str:
    W, bw, bh, gap, x0, y0, rowgap = 680, 196, 92, 26, 20, 16, 44
    H = y0 + 2 * bh + rowgap + 46
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
           f'font-family="DM Sans, system-ui, sans-serif">',
           f'<title>{escape(LEGEND[lang])}</title>',
           '<defs><marker id="pm-arr" viewBox="0 0 10 10" refX="8" refY="5" '
           'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           f'<path d="M2 1L8 5L2 9" fill="none" stroke="{INK2}" stroke-width="1.5"/>'
           '</marker></defs>',
           f'<rect width="{W}" height="{H}" fill="#FAFAF8"/>']
    boxes = []
    for i, (title, lines, llm) in enumerate(STEPS[lang]):
        r, c = divmod(i, 3)
        bx, by = x0 + c * (bw + gap), y0 + r * (bh + rowgap)
        boxes.append((bx, by))
        fill, stroke = ("#F5ECD8", "#C4914A") if llm else ("#F3EFE6", "#C8C2B4")
        out.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="6" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
        out.append(f'<text x="{bx + 14}" y="{by + 26}" font-size="14" font-weight="500" '
                   f'fill="{INK}">{escape(title)}</text>')
        for j, line in enumerate(lines):
            out.append(f'<text x="{bx + 14}" y="{by + 50 + j * 20}" font-size="11.5" '
                       f'fill="{INK2}">{escape(line)}</text>')
    for i in (0, 1, 3, 4):
        bx, by = boxes[i]
        out.append(f'<line x1="{bx + bw + 3}" y1="{by + bh / 2}" x2="{bx + bw + gap - 4}" '
                   f'y2="{by + bh / 2}" stroke="{INK2}" stroke-width="1.2" '
                   f'marker-end="url(#pm-arr)"/>')
    (x3, y3), (x4, y4) = boxes[2], boxes[3]
    ym = y3 + bh + rowgap / 2
    out.append(f'<path d="M{x3 + bw / 2} {y3 + bh + 3} V{ym} H{x4 + bw / 2} V{y4 - 4}" '
               f'fill="none" stroke="{INK2}" stroke-width="1.2" marker-end="url(#pm-arr)"/>')
    out.append(f'<text x="{W / 2}" y="{H - 14}" font-size="11" fill="{INK3}" '
               f'text-anchor="middle">{escape(LEGEND[lang])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def write_pipeline() -> None:
    img = ROOT / "docs/img"
    img.mkdir(parents=True, exist_ok=True)
    for lang in ("en", "es"):
        path = img / f"pipeline_{lang}.svg"
        path.write_text(pipeline_svg(lang))
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-z", "2", str(path), "-o",
                            str(path.with_suffix(".png"))], check=True)
        print("→", path)


if __name__ == "__main__":
    main()
    write_pipeline()
