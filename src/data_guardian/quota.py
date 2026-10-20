"""Single source of truth for quota evaluation.

Both the daemon startup path and the running loop call :func:`evaluate`.
No other module may re-implement this arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database

MB = 1024 * 1024

Level = Literal["ok", "warning", "exceeded"]
Reason = Optional[Literal["daily", "monthly"]]


@dataclass(frozen=True)
class QuotaVerdict:
    daily_used: int
    daily_limit: int          # configured daily limit (0 = unlimited)
    daily_effective: int      # limit + allowance (0 = unlimited)
    daily_pct: float
    monthly_used: int
    monthly_limit: int        # 0 = unlimited
    monthly_pct: float
    level: Level
    reason: Reason
    enforce: bool             # should traffic actually be blocked?

    @property
    def blocked(self) -> bool:
        return self.reason is not None and self.enforce


def evaluate(
    db: Database,
    config: ConfigManager,
    target_date: Optional[str] = None,
) -> QuotaVerdict:
    """Compute the current quota verdict.

    Rules:
      * A limit of ``0`` means *unlimited* — never "exhausted".
      * Daily usage is the sum of **metered** rows only.
      * Daily and monthly are evaluated independently (a monthly-only plan works).
      * ``enforce`` comes from the configured action; it is not implied by being over.
    """
    day = target_date or date.today().isoformat()

    daily_limit = config.daily_limit_mb * MB
    monthly_limit = config.monthly_limit_mb * MB

    state = db.get_quota_state(day)
    # Keep the stored limit in sync with the configuration.
    if daily_limit > 0 and state.get("daily_limit_bytes") != daily_limit:
        db.set_daily_limit(daily_limit, day)
        state = db.get_quota_state(day)

    allowance = int(state.get("allowance_bytes", 0) or 0)
    daily_effective = (daily_limit + allowance) if daily_limit > 0 else 0
    daily_used = db.metered_total(day)

    monthly_used = db.metered_total_cycle(config.billing_cycle_day, day)

    daily_hit = daily_effective > 0 and daily_used >= daily_effective
    monthly_hit = monthly_limit > 0 and monthly_used >= monthly_limit

    reason: Reason = "monthly" if monthly_hit else ("daily" if daily_hit else None)

    daily_pct = (daily_used / daily_effective * 100) if daily_effective else 0.0
    monthly_pct = (monthly_used / monthly_limit * 100) if monthly_limit else 0.0
    pct = max(daily_pct, monthly_pct)

    thresholds = config.warning_thresholds
    level: Level = "ok"
    if reason is not None:
        level = "exceeded"
    elif any(pct >= t for t in thresholds):
        level = "warning"

    if reason is not None:
        db.set_quota_exceeded(True, reason, day)

    return QuotaVerdict(
        daily_used=daily_used,
        daily_limit=daily_limit,
        daily_effective=daily_effective,
        daily_pct=daily_pct,
        monthly_used=monthly_used,
        monthly_limit=monthly_limit,
        monthly_pct=monthly_pct,
        level=level,
        reason=reason,
        enforce=reason is not None and config.enforce_quota,
    )


def newly_fired_thresholds(
    db: Database, config: ConfigManager, verdict: QuotaVerdict, day: Optional[str] = None
) -> list[int]:
    """Return thresholds that have been crossed but not yet announced today."""
    day = day or date.today().isoformat()
    state = db.get_quota_state(day)
    already = {int(x) for x in (state.get("warnings_fired") or "").split(",") if x}
    return [t for t in config.warning_thresholds if verdict.daily_pct >= t and t not in already]
