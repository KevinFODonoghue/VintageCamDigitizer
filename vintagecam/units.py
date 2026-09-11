"""Formatting helpers for sizes, durations and rates shown in the UI."""

from __future__ import annotations

import math

#: Sizes use binary units (1 GB = 1024³ bytes) because that's what Windows
#: Explorer shows, so the numbers here match what you see on the drive.
GB = 1024**3


def format_bytes(n: float | None) -> str:
    if n is None or math.isnan(n):
        return "—"
    for unit, size in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if abs(n) >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} B"


def format_duration(seconds: float | None) -> str:
    """``h:mm:ss``."""
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "—"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_time_left(seconds: float | None) -> str:
    """Rough "how long until the disk is full": ``≈ 3.2 h`` or ``≈ 45 min``."""
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "—"
    if seconds >= 3600:
        return f"≈ {seconds / 3600:.1f} h"
    return f"≈ {max(1, round(seconds / 60))} min"


def gb_per_hour(bytes_per_second: float | None) -> str:
    if not bytes_per_second:
        return "—"
    return f"{bytes_per_second * 3600 / GB:.1f} GB/h"
