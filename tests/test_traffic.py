"""Tests for the daed rate resolution.

daed currently leaves the top-level `uploadRate`/`downloadRate` at 0 and only
fills `samples`, so these tests pin down both the fallback and the preference
for a sane top-level value.
"""

from __future__ import annotations

import pytest

from custom_components.daed.traffic import (
    SAMPLE_WINDOW_POINTS,
    rate_from_samples,
    resolve_rate,
)


def sample(up: float, down: float = 0) -> dict[str, float]:
    """Return one daed sample point."""
    return {"uploadRate": up, "downloadRate": down}


@pytest.mark.parametrize(
    ("top_level", "samples", "expected"),
    [
        # daed as observed: top-level mostly 0, samples carry the traffic.
        # A 0 tick means "nothing observed in that tick", not "idle now".
        (0, [sample(100), sample(0), sample(300)], 200.0),
        # Samples stay authoritative even when the top-level field is populated:
        # that field is unreliable on daed and briefly non-zero.
        (1234, [sample(1), sample(2), sample(3)], 2.0),
        # Without samples the top-level field is the only source.
        (1234, [], 1234.0),
        (None, None, 0.0),
        ("nonsense", None, 0.0),
        # a real idle period stays zero.
        (0, [sample(0), sample(0), sample(0)], 0.0),
    ],
)
def test_resolve_rate(top_level, samples, expected) -> None:
    """The best available rate is used."""
    assert resolve_rate(top_level, samples) == pytest.approx(expected)


def test_direction_selects_the_sample_field() -> None:
    """Upload and download read different fields from the same samples."""
    samples = [sample(100, 900), sample(200, 800)]
    assert resolve_rate(0, samples, direction="upload") == pytest.approx(150.0)
    assert resolve_rate(0, samples, direction="download") == pytest.approx(850.0)


def test_only_the_newest_samples_are_used() -> None:
    """Old samples do not smear a burst over the whole window."""
    samples = [sample(10_000_000)] * 10 + [sample(10), sample(20), sample(30)]
    assert rate_from_samples(samples) == pytest.approx(20.0)


def test_zero_ticks_are_ignored() -> None:
    """A sampling tick without traffic does not pull a busy line to zero."""
    assert rate_from_samples([sample(800), sample(0), sample(900)]) == pytest.approx(
        850.0
    )
    assert rate_from_samples([sample(0)]) == 0.0
    assert SAMPLE_WINDOW_POINTS >= 1


def test_negative_and_missing_values_are_clamped() -> None:
    """Garbage never produces negative or NaN rates."""
    assert rate_from_samples([{"uploadRate": None}, {"uploadRate": -5}]) == 0.0
    assert resolve_rate(-1, [sample(7)]) == pytest.approx(7.0)
    assert resolve_rate(-1, []) == 0.0
