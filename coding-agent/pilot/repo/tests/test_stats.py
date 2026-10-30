import pytest

from pilot_stats import mean, median, mode


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_mode():
    assert mode([1, 2, 2, 3]) == 2
