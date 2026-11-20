"""
Outlet positions per debate for one run (no API calls).

Usage:
    python3 validate/outlet_positions.py data/internal/snapshot_measure_headlines.jsonl
    python3 validate/outlet_positions.py <run>_headlines.jsonl --json out.json

Prints, for every debate: how much of the stance variance the outlet
explains (eta²), how often chance produces that much (permutation p), and
each outlet's mean stance with a bootstrap 90 % interval.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.outlets import all_debates, load_headlines  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("headlines", help="<run>_headlines.jsonl")
    ap.add_argument("--min-n", type=int, default=3,
                    help="minimum headlines per outlet and debate (default 3)")
    ap.add_argument("--json", default=None, help="write results to this file")
    args = ap.parse_args()

    rows = load_headlines(args.headlines)
    results = all_debates(rows, min_n=args.min_n)

    for d in results:
        if d.n_outlets == 0:
            continue
        verdict = ("outlets clearly positioned" if d.p_value < 0.01 else
                   "weak evidence" if d.p_value < 0.10 else
                   "not distinguishable from chance")
        print(f"\n{d.topic}")
        print(f"  {d.n_headlines} headlines · {d.n_outlets} outlets · "
              f"outlet explains {d.eta2:.0%} of stance variance "
              f"(chance ≈ {d.eta2_null:.0%}, p = {d.p_value:.4f}) → {verdict}")
        for o in d.positions:
            print(f"    {o.mean:+.2f}  [{o.lo:+.2f}, {o.hi:+.2f}]  "
                  f"n={o.n:<3d} {o.domain}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            [asdict(d) for d in results], ensure_ascii=False, indent=2))
        print(f"\n→ {args.json}")


if __name__ == "__main__":
    main()
