# SPDX-License-Identifier: Apache-2.0
"""Rejection thresholds, the ``ot`` tracestate member, and randomness. See docs/design.md#policy-mode.

A span is kept when its 56-bit randomness R is at or above the rejection threshold T, as in the OpenTelemetry
tracestate probability sampling specification.
"""

from __future__ import annotations

import math
import re
from typing import Final

MAX_THRESHOLD: Final = 1 << 56
"""The threshold that keeps nothing: every randomness value is below it."""

_MAX_DIGITS: Final = 14
_MAX_OT_LENGTH: Final = 256
_TH: Final = re.compile(r"[0-9a-f]{1,14}")
_RV: Final = re.compile(r"[0-9a-f]{14}")


def threshold(probability: float, precision: int) -> int:
    """The rejection threshold for a sampling probability in [0, 1].

    ``2**56 - round(probability * 2**56)``, rounded half up to ``precision`` hexadecimal digits counted after the
    leading ``f`` digits, and to 14 digits at most.
    """
    if probability <= 0.0:
        return MAX_THRESHOLD
    if probability >= 1.0:
        return 0
    # Multiplying by a power of two is exact, and so is everything after it.
    scaled = probability * MAX_THRESHOLD
    whole = math.floor(scaled)
    value = MAX_THRESHOLD - (whole + (1 if scaled - whole >= 0.5 else 0))
    _, exponent = math.frexp(probability)
    shift = 4 * (_MAX_DIGITS - min(_MAX_DIGITS, precision + (-exponent) // 4))
    if shift > 0:
        value = ((value + (1 << (shift - 1))) >> shift) << shift
    return value


def proportional(arriving: int, ratio: float, precision: int) -> int:
    """The threshold for the arriving probability multiplied by ``ratio`` (SAMPLING_MODE_PROPORTIONAL)."""
    return threshold(float(MAX_THRESHOLD - arriving) / MAX_THRESHOLD * ratio, precision)


def parse_ot(ot: str | None) -> tuple[int | None, int | None]:
    """``(rv, th)`` from the value of the ``ot`` tracestate member. An invalid sub-key reads as absent."""
    if ot is None or len(ot) > _MAX_OT_LENGTH:
        return None, None
    rv: int | None = None
    th: int | None = None
    for member in ot.split(";"):
        if member.startswith("th:"):
            value = member[3:]
            th = int(value.ljust(_MAX_DIGITS, "0"), 16) if _TH.fullmatch(value) else None
        elif member.startswith("rv:"):
            value = member[3:]
            rv = int(value, 16) if _RV.fullmatch(value) else None
    return rv, th


def randomness(trace_id: int, rv: int | None) -> int:
    """The explicit randomness value when there is one, else trace-id bytes 9-15."""
    return rv if rv is not None else trace_id & (MAX_THRESHOLD - 1)


def erase_th(ot: str) -> str | None:
    """``ot`` without its ``th`` sub-key, or None when nothing else is left."""
    return ";".join(member for member in ot.split(";") if not member.startswith("th:")) or None
