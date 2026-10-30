"""Small statistics helpers."""
from collections import Counter


def mean(values):
    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / len(values)


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def mode(values):
    if not values:
        raise ValueError("mode() requires at least one value")
    return Counter(values).most_common(1)[0][0]
