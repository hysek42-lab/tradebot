"""Trading strategies as pure functions (no I/O), so they are testable.

Active: momentum breakout swing trading.
  Entry: price breaks above the highest high of the last N completed sessions,
  while the stock is in an uptrend (last close above a rising SMA), volatile
  enough to move (ATR%) but not absurdly so, liquid, and not penny-priced.
  Candidates are ranked by 3-month momentum.
  Optional "spike" entry (spike_min_gain_pct): a large gain today qualifies even
  without an uptrend or breakout, with a wider stop.
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
    kind: str = "breakout"


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

        day_gain = price / last_close - 1
        common = (
            a["min_atr_pct"] <= atr_pct <= a["max_atr_pct"],
            price >= a["min_price"],
            dollar_vol >= a["min_dollar_volume"],
        )
        if not all(common):
            continue
        breakout = (
            price > breakout_level
            and last_close > float(trend.iloc[-1])
            and float(trend.iloc[-1]) > float(trend.iloc[-6])          # SMA rising
            and price < breakout_level * (1 + a.get("max_chase_pct", 0.08))  # don't chase gaps
        )
        # spike: a big move today counts on its own, even without an uptrend (e.g. NVDA +20 %)
        spike_min = a.get("spike_min_gain_pct")
        spike = bool(spike_min) and day_gain >= spike_min
        if not (breakout or spike):
            continue
        momentum = price / float(close.iloc[-63]) - 1
        if spike and not breakout:
            # wider stop: give back at most ~40 % of today's move, never less than k*ATR
            stop = price - max(a["stop_atr_mult"] * a_now, 0.4 * (price - last_close))
            score = 2 * day_gain
        else:
            stop = price - a["stop_atr_mult"] * a_now
            score = momentum
        out.append(Candidate(sym, price, a_now, stop, score, "spike" if spike and not breakout else "breakout"))
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


def dip_candidates(daily: dict[str, pd.DataFrame], prices: dict[str, float],
                   d: dict) -> list[Candidate]:
    """Stocks that fell hard over the last few sessions (buy the dip, no stop-loss)."""
    out = []
    for sym, df in daily.items():
        price = prices.get(sym)
        if price is None or len(df) < 30:
            continue
        close = df["Close"]
        recent_high = float(close.iloc[-d["drop_days"]:].max())
        drop = price / recent_high - 1
        dollar_vol = float((close * df["Volume"]).iloc[-20:].mean())
        if (drop <= -d["min_drop_pct"]
                and price < float(sma(close, 10).iloc[-1])
                and price >= d.get("min_price", 5)
                and dollar_vol >= d.get("min_dollar_volume", 20_000_000)):
            out.append(Candidate(sym, price, 0.0, 0.0, -drop, "dip"))
    return sorted(out, key=lambda c: c.score, reverse=True)


def dip_exit(pos: dict, price: float, d: dict) -> str | None:
    """Sell only at the profit target. Never at a loss (by design)."""
    if price >= pos["entry_price"] * (1 + d["take_profit_pct"]):
        return f"cíl +{d['take_profit_pct']:.0%}"
    return None
