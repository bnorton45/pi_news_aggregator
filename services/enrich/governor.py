"""Adaptive admission control — embed-budget governor (PLAN §6.3a).

Closed loop that holds the *social-class* embed survivor rate at the RAM-derived
budget (default 400k/day, PLAN §2) with no operator intervention:

  * controlled variable : EWMA of admitted social survivors (events/sec)
  * actuator            : admission threshold theta over a [0,1] relevance score
                          ("admit relevance >= theta" == admit the top tail)
  * control law         : AIMD with deadband (ignore noise) + slew limit (no oscillation)

Authoritative/primary items BYPASS the governor entirely. `sampling_active` mirrors
"are we currently shedding?" so downstream scoring can self-discount (PLAN §6.3a).
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from libs.schema import SourceClass


@dataclass(frozen=True)
class GovernorConfig:
    cap_per_day: int = 400_000  # PLAN §2 RAM-derived budget (config, not hardcoded policy)
    ewma_halflife_s: float = 600.0  # ~10 min rate smoothing
    deadband: float = 0.10  # +/-10% around budget => no action
    theta_floor: float = 0.0
    theta_ceiling: float = 0.99
    additive_step: float = 0.02  # tighten increment when over budget
    relax_rate: float = 0.05  # fraction of (theta-floor) shed per step when under budget
    slew_max: float = 0.05  # max |delta theta| per update
    explore_rate: float = 0.02  # §6.3a exploration quota: ~2% of the shed tail

    @property
    def budget_per_s(self) -> float:
        return self.cap_per_day / 86_400.0


class AdmissionGovernor:
    def __init__(self, cfg: GovernorConfig | None = None) -> None:
        self.cfg = cfg or GovernorConfig()
        self.theta = self.cfg.theta_floor
        self._rate = 0.0  # EWMA admitted social survivors, events/sec
        self._last_t: float | None = None

    @property
    def sampling_active(self) -> bool:
        return self.theta > self.cfg.theta_floor + 1e-9

    @property
    def at_ceiling(self) -> bool:
        """Sustained over-budget: theta pinned at its ceiling (the 'needs another
        node' regime, §6.3a) — no spare embed budget for anything optional."""
        return self.theta >= self.cfg.theta_ceiling - 1e-9

    def explore(self) -> bool:
        """§6.3a exploration quota, called on a SHED item: embed it anyway with
        probability explore_rate, tagged `exploration`. These shed-tail
        counterfactuals are the negative/uncertain labels the filter retrain loop
        needs — without them weak-labeling only sees admitted items and the
        classifier collapses to relearning its own decisions. Off at the ceiling
        (no spare budget to explore with)."""
        if self.at_ceiling:
            return False
        return random.random() < self.cfg.explore_rate  # noqa: S311 — sampling, not crypto

    @property
    def rate_per_s(self) -> float:
        return self._rate

    def _decay(self, dt: float) -> float:
        return math.exp(-dt * math.log(2) / self.cfg.ewma_halflife_s)

    def admit(
        self, *, relevance: float, source_class: SourceClass, now: float | None = None
    ) -> bool:
        """Decide admission for one item and step the controller.

        `relevance` in [0,1] from the cheap filter. Non-social classes bypass and
        do not consume the social budget.
        """
        if source_class in (SourceClass.AUTHORITATIVE, SourceClass.PRIMARY):
            return True

        now = time.monotonic() if now is None else now
        admitted = relevance >= self.theta

        # EWMA of admitted rate, sampled on EVERY event (admitted -> 1/dt, else 0).
        # Updating each call keeps the estimate smooth so the loop stays damped.
        if self._last_t is not None:
            dt = max(now - self._last_t, 1e-6)
            decay = self._decay(dt)
            sample = (1.0 / dt) if admitted else 0.0
            self._rate = decay * self._rate + (1.0 - decay) * sample
        self._last_t = now

        self._step(now)
        return admitted

    def _step(self, now: float) -> None:
        budget = self.cfg.budget_per_s
        hi = budget * (1.0 + self.cfg.deadband)
        lo = budget * (1.0 - self.cfg.deadband)
        cfg = self.cfg

        if self._rate > hi:  # over budget -> tighten (additive increase)
            step = cfg.additive_step
        elif self._rate < lo:  # under budget -> relax toward floor (multiplicative)
            step = -(self.theta - cfg.theta_floor) * cfg.relax_rate
        else:
            step = 0.0

        step = max(-cfg.slew_max, min(cfg.slew_max, step))  # slew limit
        self.theta = max(cfg.theta_floor, min(cfg.theta_ceiling, self.theta + step))
