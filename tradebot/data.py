"""Market data via yfinance (free, delayed ~15 min for some exchanges).

Trading 212's API has no price history endpoint, so signals use Yahoo data.
Interface kept small so tests can swap in a fake.
"""
import logging
import time
from datetime import date, datetime, timezone

import pandas as pd

log = logging.getLogger(__name__)


class MarketData:
    def __init__(self):
        import yfinance  # imported lazily so tests don't need it
        self.yf = yfinance
        self._daily_cache: dict[str, pd.DataFrame] = {}
        self._daily_day: date | None = None
        self._fx_cache: dict[str, tuple[float, float]] = {}
        self._ccy_cache: dict[str, str] = {}

    def daily(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """~1 year of daily OHLCV bars, completed sessions only, cached per day."""
        today = date.today()
        if self._daily_day != today:
            self._daily_cache.clear()
            self._daily_day = today
        missing = [s for s in symbols if s not in self._daily_cache]
        if missing:
            raw = self.yf.download(missing, period="1y", interval="1d", auto_adjust=True,
                                   group_by="ticker", progress=False, threads=True)
            for s in missing:
                try:
                    df = raw[s] if len(missing) > 1 else raw
                    if isinstance(df.columns, pd.MultiIndex):
                        df = df.droplevel(0, axis=1)
                    df = df.dropna(subset=["Close"])
                    # drop today's partial bar; signals use completed sessions
                    if len(df) and df.index[-1].date() >= today:
                        df = df.iloc[:-1]
                    if len(df) > 60:
                        self._daily_cache[s] = df
                except KeyError:
                    log.warning("no daily data for %s", s)
        return {s: self._daily_cache[s] for s in symbols if s in self._daily_cache}

    def intraday(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Today's 5-minute bars (index in UTC)."""
        if not symbols:
            return {}
        raw = self.yf.download(symbols, period="1d", interval="5m", auto_adjust=True,
                               group_by="ticker", progress=False, threads=True)
        out = {}
        for s in symbols:
            try:
                df = raw[s] if len(symbols) > 1 else raw
                if isinstance(df.columns, pd.MultiIndex):
                    df = df.droplevel(0, axis=1)
                df = df.dropna(subset=["Close"])
                if len(df):
                    if df.index.tz is None:
                        df.index = df.index.tz_localize("UTC")
                    out[s] = df
            except KeyError:
                pass
        return out

    def last_prices(self, symbols: list[str]) -> dict[str, tuple[float, datetime]]:
        """Latest 5-minute close and its timestamp (UTC) for each symbol."""
        if not symbols:
            return {}
        raw = self.yf.download(symbols, period="1d", interval="5m", auto_adjust=True,
                               group_by="ticker", progress=False, threads=True)
        out = {}
        for s in symbols:
            try:
                df = raw[s] if len(symbols) > 1 else raw
                if isinstance(df.columns, pd.MultiIndex):
                    df = df.droplevel(0, axis=1)
                df = df.dropna(subset=["Close"])
                if len(df):
                    ts = df.index[-1].to_pydatetime()
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    out[s] = (float(df["Close"].iloc[-1]), ts.astimezone(timezone.utc))
            except KeyError:
                pass
        return out

    def currency(self, symbol: str) -> str:
        """Trading currency of a Yahoo symbol (e.g. USD, EUR, GBp)."""
        if symbol not in self._ccy_cache:
            ccy = self.yf.Ticker(symbol).fast_info.get("currency") or "USD"
            self._ccy_cache[symbol] = "GBX" if ccy in ("GBp", "GBX") else ccy.upper()
        return self._ccy_cache[symbol]

    def fx(self, from_ccy: str, to_ccy: str) -> float:
        """Units of to_ccy per 1 from_ccy. Cached for 1 hour."""
        if from_ccy == to_ccy:
            return 1.0
        if from_ccy == "GBX":  # London pence
            return self.fx("GBP", to_ccy) / 100
        key = f"{from_ccy}{to_ccy}"
        hit = self._fx_cache.get(key)
        if hit and time.time() - hit[1] < 3600:
            return hit[0]
        df = self.yf.download(f"{key}=X", period="5d", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        rate = float(df["Close"].dropna().iloc[-1])
        self._fx_cache[key] = (rate, time.time())
        return rate
