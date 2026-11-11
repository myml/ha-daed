"""Turn daed's runtime traffic payload into usable rates.

daed's ``runtimeOverview`` reports the instantaneous rate twice:

* the top-level ``uploadRate`` / ``downloadRate`` fields, and
* a ``samples`` list of ``(timestamp, uploadRate, downloadRate)`` points that
  it fills on its own background cadence.

On the daed builds this integration was validated against the top-level fields
are unreliable: they read ``0`` most of the time and only occasionally carry a
value, while ``samples`` always reflects the real traffic (it is what daed's own
dashboard plots). ``samples`` is therefore authoritative, and the top-level
field is only used when daed returned no samples at all.

A single sample can read ``0`` mid-transfer even while traffic flows, so the
most recent samples are combined instead of trusting the last point alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

# How many of the newest samples to look at. daed samples roughly every 20 s,
# so three points cover about a minute without smearing a burst over too long.
SAMPLE_WINDOW_POINTS: Final = 3


SAMPLE_UPLOAD_KEY: Final = "uploadRate"
SAMPLE_DOWNLOAD_KEY: Final = "downloadRate"


def resolve_rate(
    top_level: Any,
    samples: Iterable[Mapping[str, Any]] | None,
    *,
    direction: str = "upload",
) -> float:
    """Return the best available rate in bytes per second.

    ``samples`` wins whenever daed returned any: its top-level
    ``uploadRate``/``downloadRate`` fields are mostly ``0`` on the daed versions
    this was validated against. ``top_level`` is only a fallback for a payload
    without samples. ``direction`` selects the sample field to read
    (``upload`` or ``download``).
    """
    points = list(samples or [])
    if points:
        return rate_from_samples(points, direction=direction)
    return _as_rate(top_level)


def rate_from_samples(
    samples: Iterable[Mapping[str, Any]] | None, *, direction: str = "upload"
) -> float:
    """Estimate the current rate from daed's timestamped samples.

    The newest ``SAMPLE_WINDOW_POINTS`` samples are averaged. daed emits ``0``
    for a sampling tick that observed no traffic (and when its sampler has not
    been warmed up yet), which would drag an otherwise busy line down, so zero
    points are ignored. When every point is zero the line really is idle and
    the result is ``0``.
    """
    key = SAMPLE_UPLOAD_KEY if direction == "upload" else SAMPLE_DOWNLOAD_KEY
    window = [
        _as_rate(sample.get(key))
        for sample in list(samples or [])[-SAMPLE_WINDOW_POINTS:]
    ]
    active = [point for point in window if point > 0]
    if not active:
        return 0.0
    return sum(active) / len(active)


def _as_rate(value: Any) -> float:
    """Coerce a rate to a non-negative float, treating junk as zero."""
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0
