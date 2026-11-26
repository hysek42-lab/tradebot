"""Trading strategies as pure functions (no I/O), so they are testable.

Active: momentum breakout swing trading.
  Entry: price breaks above the highest high of the last N completed sessions,
  while the stock is in an uptrend (last close above a rising SMA), volatile
  enough to move (ATR%) but not absurdly so, liquid, and not penny-priced.
  Candidates are ranked by 3-month momentum.
  Exit: initial stop at entry - k*ATR, then a trailing stop at the highest
  price since entry minus m*ATR (never lowered), plus a max holding period.

Core: gradual ETF accumulation toward a target weight, buying the full
  shortfall when the ETF is in a dip (>= X% below its 1-year high).
"""
from dataclasses import dataclass

import pandas as pd

from .indicators import atr, sma


@dataclass
class Candidate:
    symbol: str
    price: float
    atr: float
    stop: float
    score: float


def active_candidates(daily: dict[str, pd.DataFrame], prices: dict[str, float],
                      cfg: dict) -> list[Candidate]:
    a = cfg["active"]
    out = []
    for sym, df in daily.items():
        price = prices.get(sym)
        if price is None or len(df) < max(a["trend_sma_days"] + 5, 70):
            continue
        close = df["Close"]
        trend = sma(close, a["trend_sma_days"])
        a_now = float(atr(df, a["atr_days"]).iloc[-1])
        last_close = float(close.iloc[-1])
        breakout_level = float(df["High"].iloc[-a["breakout_lookback_days"]:].max())
        dollar_vol = float((close * df["Volume"]).iloc[-20:].mean())
        atr_pct = a_now / last_close

        conditions = (
            price > breakout_level,
            last_close > float(trend.iloc[-1]),
            float(trend.iloc[-1]) > float(trend.iloc[-6]),        # SMA rising
            a["min_atr_pct"] <= atr_pct <= a["max_atr_pct"],
            price >= a["min_price"],
            dollar_vol >= a["min_dollar_volume"],
            price < breakout_level * 1.08,                          # don't chase gaps
        )
        if not all(conditions):
            continue
        momentum = price / float(close.iloc[-63]) - 1
        stop = price - a["stop_atr_mult"] * a_now
        out.append(Candidate(sym, price, a_now, stop, momentum))
    return sorted(out, key=lambda c: c.score, reverse=True)


def update_exit(pos: dict, price: float, days_held: float, cfg: dict
                ) -> tuple[float, float, str | None]:
    """Returns (new_stop, new_highest, exit_reason or None)."""
    a = cfg["active"]
    highest = max(pos["highest"] or pos["entry_price"], price)
    trail = highest - a["trail_atr_mult"] * pos["atr"]
    stop = max(pos["stop"], trail)          # stop only ever moves up
    if price <= stop:
        return stop, highest, "stop-loss" if stop <= pos["entry_price"] else "posuvný stop"
    if days_held >= a["max_hold_days"]:
        return stop, highest, "max. doba držení"
    return stop, highest, None


def core_buys(core_capital: float, held_value: dict[str, float],
              etf_stats: dict[str, tuple[float, float]], cfg: dict) -> dict[str, float]:
    """Value (account currency) to buy per ETF isin.

    held_value: isin -> current value held (account currency)
    etf_stats:  isin -> (price, 1-year high) in instrument currency
    """
    c = cfg["core"]
    buys = {}
    for etf in c["etfs"]:
        isin = etf["isin"]
        if isin not in etf_stats:
            continue
        price, high = etf_stats[isin]
        target = core_capital * etf["weight"]
        shortfall = target - held_value.get(isin, 0.0)
        if shortfall <= 0:
            continue
        in_dip = price <= high * (1 - c["dip_threshold_pct"])
        value = shortfall if in_dip else shortfall * c["normal_fill_fraction"]
        if value >= cfg["risk"]["min_order_value"]:
            buys[isin] = value
    return buys
