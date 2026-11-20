"""
Checks for analysis/outlets.py with synthetic data (no network, no API).

    cd analysis && python3 test_outlets.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.outlets import debate_outlets, eta_squared  # noqa: E402


def rows_for(outlet_means: dict[str, float], n: int, noise: float,
             seed: int = 0) -> list[dict]:
    rng = np.random.default_rng(seed)
    out = []
    for dom, mu in outlet_means.items():
        for s in np.clip(mu + rng.normal(0, noise, n), -1, 1):
            out.append({"domain": dom, "topic": "t", "stance": float(s)})
    return out


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(1)


print("outlets")

# Outlets with real, distinct positions → strong effect, tiny p.
pos = rows_for({"a": -0.6, "b": -0.5, "c": 0.0, "d": 0.5, "e": 0.6},
               n=12, noise=0.35)
d = debate_outlets(pos, "t", n_perm=1000, n_boot=500)
check("distinct outlets: p < 0.01", d.p_value < 0.01)
check("distinct outlets: eta² well above chance", d.eta2 > 2 * d.eta2_null)
check("positions sorted from one pole to the other",
      [o.domain for o in d.positions][0] in ("a", "b")
      and [o.domain for o in d.positions][-1] in ("d", "e"))
check("each interval contains its mean",
      all(o.lo <= o.mean <= o.hi for o in d.positions))

# Same noise, every outlet at 0 → no outlet effect.
flat = rows_for({k: 0.0 for k in "abcde"}, n=12, noise=0.35, seed=1)
d0 = debate_outlets(flat, "t", n_perm=1000, n_boot=200)
check("identical outlets: p > 0.05", d0.p_value > 0.05)

# Outlets below min_n are dropped.
few = pos + [{"domain": "tiny", "topic": "t", "stance": 1.0}]
check("outlet with 1 headline excluded",
      "tiny" not in [o.domain for o in debate_outlets(
          few, "t", min_n=3, n_perm=100, n_boot=50).positions])

check("eta² is 0 when all values equal",
      eta_squared(np.array(["a", "b"]), np.array([0.5, 0.5])) == 0.0)

print("\nRESULTADO: TODO PASA")
