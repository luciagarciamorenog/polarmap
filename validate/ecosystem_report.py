"""
Informe del ecosistema de medios para una corrida (sin llamadas a la API).

    python3 validate/ecosystem_report.py data/internal/<run>_headlines.jsonl
    python3 validate/ecosystem_report.py <run>_headlines.jsonl --json out.json

Por debate: índice por titular y con un medio un voto, cuánto explica el
periódico, distancia entre los dos bandos de medios frente al azar, número
de grupos, hostilidad de los extremos y opinión frente a noticia. Para toda
la corrida: eje Gobierno ↔ oposición (si existe) y polarización de agenda.
Detalle en analysis/ecosystem.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.ecosystem import ecosystem_report, load_records, summary_lines  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("headlines", help="<run>_headlines.jsonl")
    ap.add_argument("--country", default=None, help="código de país (por defecto, todos)")
    ap.add_argument("--json", default=None, help="guardar el informe en este fichero")
    args = ap.parse_args()

    rep = ecosystem_report(load_records(args.headlines), country=args.country)
    print("\n".join(summary_lines(rep)))
    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(f"\n→ {args.json}")


if __name__ == "__main__":
    main()
