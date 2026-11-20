"""
Outlet-level stance: where each media outlet sits on a debate's axis.

Why this exists
---------------
A single headline's stance is noisy: against reference labels the model's
per-headline stance correlates only ~0.46 and it pulls ambiguous headlines
towards the centre. But an outlet publishes many headlines on the same
debate, and averaging them cancels much of that noise. So instead of asking
"is this headline on side A or B?", this module asks two questions that the
data can answer with confidence:

1. Does the outlet a headline comes from predict its stance?
   (eta², with a permutation test: shuffle stances across outlets and see
   how often chance alone produces an outlet effect this large.)
2. Where does each outlet sit on the axis, and how sure are we?
   (mean stance per outlet with a bootstrap 90 % interval.)

If outlets line up consistently and far beyond chance, the press is
positioned on that debate. Whether those positions form two separate camps
(polarization) is then a question about outlets, not about single headlines.

Input: the per-headline file each run writes to
data/internal/<run>_headlines.jsonl (fields: domain, topic, stance,
off_topic). No API calls.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class OutletPosition:
    domain: str
    n: int
    mean: float
    lo: float      # bootstrap 5th percentile
    hi: float      # bootstrap 95th percentile


@dataclass
class DebateOutlets:
    topic: str
    n_headlines: int
    n_outlets: int
    eta2: float            # share of stance variance explained by outlet
    eta2_null: float       # mean eta² when stances are shuffled
    p_value: float         # permutation p-value for the outlet effect
    positions: list[OutletPosition] = field(default_factory=list)


def load_headlines(path: str | Path) -> list[dict]:
    """On-topic headlines with a numeric stance."""
    rows = [json.loads(line) for line in Path(path).read_text().splitlines()
            if line.strip()]
    return [r for r in rows
            if r.get("topic") and not r.get("off_topic")
            and isinstance(r.get("stance"), (int, float))]


def eta_squared(groups: np.ndarray, values: np.ndarray) -> float:
    """Between-group sum of squares over total sum of squares."""
    grand = values.mean()
    sst = float(((values - grand) ** 2).sum())
    if sst == 0:
        return 0.0
    ssb = sum(float((groups == g).sum()) * (values[groups == g].mean() - grand) ** 2
              for g in np.unique(groups))
    return ssb / sst


def debate_outlets(rows: list[dict], topic: str, min_n: int = 3,
                   n_perm: int = 3000, n_boot: int = 4000,
                   seed: int = 0) -> DebateOutlets:
    """Outlet effect and outlet positions for one debate."""
    rng = np.random.default_rng(seed)
    sel = [r for r in rows if r["topic"] == topic]
    counts = Counter(r["domain"] for r in sel)
    sel = [r for r in sel if counts[r["domain"]] >= min_n]
    if not sel:
        return DebateOutlets(topic, 0, 0, float("nan"), float("nan"), float("nan"))

    groups = np.array([r["domain"] for r in sel])
    values = np.array([r["stance"] for r in sel], dtype=float)

    obs = eta_squared(groups, values)
    null = np.array([eta_squared(groups, rng.permutation(values))
                     for _ in range(n_perm)])
    p = float((np.sum(null >= obs) + 1) / (n_perm + 1))

    by_outlet: dict[str, list[float]] = defaultdict(list)
    for g, v in zip(groups, values):
        by_outlet[g].append(v)
    positions = []
    for dom, vals in by_outlet.items():
        v = np.asarray(vals)
        boots = [rng.choice(v, len(v)).mean() for _ in range(n_boot)]
        positions.append(OutletPosition(dom, len(v), float(v.mean()),
                                        float(np.percentile(boots, 5)),
                                        float(np.percentile(boots, 95))))
    positions.sort(key=lambda o: o.mean)

    return DebateOutlets(topic, len(values), len(by_outlet), obs,
                         float(null.mean()), p, positions)


def all_debates(rows: list[dict], **kw) -> list[DebateOutlets]:
    topics = [t for t, _ in Counter(r["topic"] for r in rows).most_common()]
    return [debate_outlets(rows, t, **kw) for t in topics]
