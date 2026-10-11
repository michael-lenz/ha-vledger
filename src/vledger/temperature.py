# SPDX-License-Identifier: BSD-3-Clause
"""Fuel volumes at 15 °C (VER-08, ADR-0040): the outside temperature
through a low-pass filter, and a fuel delta normalised with it.

V₁₅ = V / (1 + β·(T − 15)), β the fuel's ``beta_per_k``, T the outside
temperature filtered with time constant ``temperature_tau_s``. The filter
is first order and exact for a value that holds until the next sample:
between two samples the filtered value y moves toward the held value x as
y ← x + (y − x)·e^(−Δt/τ). It runs over a fixed window of five time
constants before the time asked for, started at the value in effect at the
window's start, so its answer is a function of L0 alone — the same whether
a derivation reads the whole stream or from a cursor (ABL-05, ABL-06).
Nothing is read across a dropout or a capture gap: one restarts the filter
at the next sample, and one after the last sample leaves no value.

The figures are plausibility only and feed nothing: consumption, periods
and receipt matching stay on the raw values and receipts (VER-01).
"""

from __future__ import annotations

import math
from datetime import timedelta

from vledger import clock, series
from vledger.series import Sample, Stream

ROLE = "outside_temperature"

#: The window, in time constants: what it cuts off weighs e⁻⁵ ≈ 0.7 % of a
#: temperature difference (ADR-0040, point 2).
WINDOW_TAUS = 5

#: The reference temperature of the normalisation, °C.
REFERENCE_C = 15.0


def normalise(litres: float, temperature_c: float, beta_per_k: float) -> float:
    """``litres`` at ``temperature_c`` as litres at 15 °C."""
    return litres / (1 + beta_per_k * (temperature_c - REFERENCE_C))


def window_start(t: str, tau_s: float) -> str:
    return clock.to_text(clock.parse(t) - timedelta(seconds=WINDOW_TAUS * tau_s))


def filtered(s: Stream, t: str, tau_s: float) -> float | None:
    """The filtered outside temperature at ``t``, or ``None`` without one:
    no sample in the window, or a dropout or gap after the last. ``s`` must
    hold the role's samples from the window's start on — a stream read
    from the beginning, or :class:`Source`'s window."""
    start = window_start(t, tau_s)
    lo, hi = clock.parse(start), clock.parse(t)
    # What happens inside the window, in time order: a sample sets the held
    # value, a dropout or the start of a gap clears it (ABL-04). A gap starts
    # at the last line before it, so a sample at that time comes first.
    events: list[tuple] = [(clock.parse(x.t), 0, x.value) for x in s.series.get(ROLE, [])
                           if lo < clock.parse(x.t) <= hi]
    events += [(clock.parse(d), 1, None) for d in s.dropouts.get(ROLE, [])
               if lo < clock.parse(d) <= hi]
    events += [(clock.parse(g.start), 1, None) for g in s.gaps
               if lo < clock.parse(g.start) <= hi]
    events.sort(key=lambda e: (e[0], e[1]))
    first = series.in_effect(s, ROLE, start)
    held = y = first.value if first else None
    now = lo

    def advance(to) -> None:
        nonlocal y, now
        if held is not None and y is not None:
            y = held + (y - held) * math.exp(-(to - now).total_seconds() / tau_s)
        now = to

    for when, clears, value in events:
        advance(when)
        if not clears:
            held = value
            if y is None:
                y = value          # the filter (re)starts at its first sample
        else:
            held = y = None
    advance(hi)
    return y if held is not None else None


class Source:
    """Where a derivation's filter reads (ADR-0040, point 2): the stream
    itself when it was read from the beginning, else the window loaded from
    L0 — once, reaching back as far as the earliest time asked for."""

    def __init__(self, s: Stream):
        self.s = s
        thr = s.thresholds()
        self.tau_s = float(thr["temperature_tau_s"])
        self.beta = float(thr["beta_per_k"])
        self._window: Stream | None = s if s.since is None else None

    def _stream_for(self, t: str) -> Stream | None:
        start = window_start(t, self.tau_s)
        w = self._window
        if w is not None and (w.since is None or clock.parse(w.since) <= clock.parse(start)):
            return w
        if self.s.base is None:
            return None
        self._window = series.load(self.s.base, self.s.subject, since=start, until=self.s.last_t)
        return self._window

    def at(self, t: str) -> float | None:
        if ROLE not in ((self.s.config or {}).get("roles") or {}):
            return None
        w = self._stream_for(t)
        return filtered(w, t, self.tau_s) if w is not None else None

    def delta_15c(self, before: Sample | None, after: Sample | None) -> float | None:
        """V₁₅ of ``after`` at its time minus V₁₅ of ``before`` at its own,
        rounded as the raw delta is; ``None`` where anything is missing."""
        if before is None or after is None:
            return None
        ta, tb = self.at(before.t), self.at(after.t)
        if ta is None or tb is None:
            return None
        return round(normalise(after.value, tb, self.beta)
                     - normalise(before.value, ta, self.beta), 3) + 0.0
