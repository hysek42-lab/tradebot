"""Risk rules: circuit breakers and position sizing. Pure functions, easy to test."""
import math
from dataclasses import dataclass


@dataclass
class Guard:
    allow_entries: bool
    halted: bool
    reason: str = ""


def check_guards(total: float, day_start: float | None, peak: float | None,
                 manual_stop: bool, cfg: dict) -> Guard:
    r = cfg["risk"]
    if manual_stop:
        return Guard(False, True, "zastaveno ručně (/stop)")
    if peak and total < peak * (1 - r["max_drawdown_halt_pct"]):
        return Guard(False, True,
                     f"pokles {1 - total / peak:.0%} z maxima, bot zastaven (/start pro obnovení)")
    if day_start and total < day_start * (1 - r["daily_loss_cap_pct"]):
        return Guard(False, False,
                     f"denní ztráta {1 - total / day_start:.1%}, dnes už žádné nové obchody")
    return Guard(True, False)


def round_down(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f) / f


def size_active(active_capital: float, available: float, entry: float, stop: float,
                fx: float, cfg: dict) -> float:
    """Quantity (shares) for an active trade.

    active_capital / available are in account currency; entry/stop in instrument
    currency; fx = account currency per 1 unit of instrument currency.
    Risk-based: losing (entry - stop) per share costs at most risk_per_trade_pct
    of active capital, capped by max_position_pct and available cash.
    """
    r = cfg["risk"]
    per_share_risk = (entry - stop) * fx
    if per_share_risk <= 0 or entry <= 0:
        return 0.0
    qty_risk = active_capital * r["risk_per_trade_pct"] / per_share_risk
    cap_value = min(active_capital * r["max_position_pct"], available)
    qty_cap = cap_value / (entry * fx)
    qty = round_down(max(0.0, min(qty_risk, qty_cap)), cfg["order"]["qty_decimals"])
    if qty * entry * fx < r["min_order_value"]:
        return 0.0
    return qty
