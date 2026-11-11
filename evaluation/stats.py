"""Paired statistics without third-party packages: Wilcoxon signed-rank test and a bootstrap interval.

wilcoxon(d): two-sided test of paired differences d (mode - baseline per case). Zero differences
are dropped (Wilcoxon's convention), ties get average ranks. The p-value is exact (enumeration of the
2^n sign assignments over the rank sums, by dynamic programming) for n <= 25 without ties, and from
the normal approximation with tie and continuity corrections otherwise.

bootstrap_ci(d): percentile interval of the mean difference over cases, resampling cases with a
fixed seed (deterministic).
"""
from __future__ import annotations

import math
import random
from typing import Sequence


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def wilcoxon(diffs: Sequence[float]) -> dict:
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return {"n": 0, "w_plus": 0.0, "p": None, "method": None}
    ranks = _ranks([abs(x) for x in d])
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    ties = len(set(abs(x) for x in d)) < n
    if n <= 25 and not ties:
        # exact: distribution of the sum of ranks 1..n with random signs
        total = n * (n + 1) // 2
        counts = [0] * (total + 1)
        counts[0] = 1
        for r in range(1, n + 1):
            for s in range(total, r - 1, -1):
                counts[s] += counts[s - r]
        w = int(round(w_plus))
        lo = sum(counts[: min(w, total - w) + 1])
        p = min(1.0, 2 * lo / 2 ** n)
        return {"n": n, "w_plus": w_plus, "p": p, "method": "exact"}
    mean = n * (n + 1) / 4
    tie_term = 0.0
    abs_vals = [abs(x) for x in d]
    for v in set(abs_vals):
        t = abs_vals.count(v)
        tie_term += t ** 3 - t
    var = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if var <= 0:
        return {"n": n, "w_plus": w_plus, "p": None, "method": "normal"}
    z = (abs(w_plus - mean) - 0.5) / math.sqrt(var)
    p = math.erfc(max(z, 0) / math.sqrt(2))
    return {"n": n, "w_plus": w_plus, "p": min(1.0, p), "method": "normal"}


def bootstrap_ci(diffs: Sequence[float], level: float = 0.95, samples: int = 10000, seed: int = 20261012) -> tuple:
    if not diffs:
        return (None, None)
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    lo = means[int((1 - level) / 2 * samples)]
    hi = means[min(samples - 1, int((1 + level) / 2 * samples))]
    return (lo, hi)
