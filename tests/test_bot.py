from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from zoneinfo import ZoneInfo

from tradebot import strategies
from tradebot.engine import Engine
from tradebot.risk import check_guards, size_active
from tradebot.state import State
from tradebot.t212 import OrderUncertain

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path):
    c = yaml.safe_load((ROOT / "config.yaml").read_text())
    c["data_dir"] = tmp_path
    c["markets"]["us"]["universe"] = ["UPPY", "DOWNY"]
    c["markets"]["eu"]["enabled"] = False
    return c


def make_df(trend: float, n=260, start=100.0, vol=0.008, seed=1):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + trend + rng.normal(0, vol, n))
    idx = pd.bdate_range(end=datetime(2026, 11, 20), periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.015, "Low": close * 0.985,
                         "Close": close, "Volume": 2_000_000}, index=idx)


# ---------------------------------------------------------------- fakes
class FakeTG:
    def __init__(self):
        self.sent, self.queue = [], []

    def send(self, t):
        self.sent.append(t)

    def commands(self):
        q, self.queue = self.queue, []
        return q


class FakeData:
    def __init__(self, daily, prices):
        self._daily, self.prices = daily, prices

    def daily(self, syms):
        return {s: self._daily[s] for s in syms if s in self._daily}

    def last_prices(self, syms):
        now = datetime.now(timezone.utc)
        return {s: (self.prices[s], now) for s in syms if s in self.prices}

    def fx(self, a, b):
        return 1.0

    def currency(self, s):
        return "USD"


class FakeBroker:
    def __init__(self, cash=10000.0):
        self.cash = cash
        self.pos = {}           # ticker -> dict(qty, avg, px)
        self.stops = {}         # id -> (ticker, qty, stop)
        self.orders = []
        self.next_id = 1
        self.fail_next_order = None
        self.prices = {}
        self.inst = [
            {"ticker": "UPPY_US_EQ", "type": "STOCK", "currencyCode": "USD", "isin": "US1"},
            {"ticker": "DOWNY_US_EQ", "type": "STOCK", "currencyCode": "USD", "isin": "US2"},
            {"ticker": "VUAAl_EQ", "type": "ETF", "currencyCode": "USD", "isin": "IE00BFMXXD54"},
            {"ticker": "VWCEd_EQ", "type": "ETF", "currencyCode": "EUR", "isin": "IE00BK5BQT80"},
        ]

    def _px(self, t):
        return self.prices.get(t, 100.0)

    def summary(self):
        inv = sum(p["qty"] * self._px(t) for t, p in self.pos.items())
        return {"currency": "USD", "totalValue": self.cash + inv,
                "cash": {"availableToTrade": self.cash}}

    def positions(self):
        return [{"instrument": {"ticker": t, "isin": next(i["isin"] for i in self.inst if i["ticker"] == t)},
                 "quantity": p["qty"], "quantityAvailableForTrading": p["qty"],
                 "averagePricePaid": p["avg"], "currentPrice": self._px(t),
                 "walletImpact": {"currentValue": p["qty"] * self._px(t)}}
                for t, p in self.pos.items()]

    def instruments(self):
        return self.inst

    def market_order(self, ticker, qty):
        if self.fail_next_order:
            e, self.fail_next_order = self.fail_next_order, None
            raise e
        self.orders.append((ticker, qty))
        px = self._px(ticker)
        p = self.pos.setdefault(ticker, {"qty": 0.0, "avg": px})
        if qty > 0:
            p["avg"] = (p["avg"] * p["qty"] + px * qty) / (p["qty"] + qty)
        p["qty"] += qty
        self.cash -= qty * px
        if p["qty"] <= 1e-9:
            del self.pos[ticker]
        return {"id": self.next_id}

    def stop_order(self, ticker, qty, stop):
        self.next_id += 1
        self.stops[self.next_id] = (ticker, qty, stop)
        return {"id": self.next_id}

    def cancel_order(self, oid):
        self.stops.pop(oid, None)


def make_engine(cfg, broker, data, when=datetime(2026, 11, 23, 11, 0, tzinfo=NY)):
    tg = FakeTG()
    eng = Engine(cfg, broker, data, State(cfg["data_dir"] / "t.sqlite"), tg,
                 clock=lambda: when, sleep=lambda s: None)
    eng.market_open = lambda m="us": True
    return eng, tg


# ---------------------------------------------------------------- unit tests
def test_breakout_detected_and_downtrend_rejected(cfg):
    up, down = make_df(0.004), make_df(-0.003)
    prices = {"UPPY": float(up["High"].iloc[-20:].max()) * 1.01,
              "DOWNY": float(down["Close"].iloc[-1])}
    cands = strategies.active_candidates({"UPPY": up, "DOWNY": down}, prices, cfg)
    assert [c.symbol for c in cands] == ["UPPY"]
    assert cands[0].stop < cands[0].price


def test_trailing_stop_only_moves_up(cfg):
    pos = {"entry_price": 100, "stop": 94, "highest": 100, "atr": 3}
    stop, high, reason = strategies.update_exit(pos, 120, 1, cfg)
    assert (stop, high, reason) == (111, 120, None)
    pos.update(stop=stop, highest=high)
    stop2, _, _ = strategies.update_exit(pos, 115, 2, cfg)
    assert stop2 == 111                      # not lowered
    _, _, reason = strategies.update_exit(pos, 110, 3, cfg)
    assert reason == "posuvný stop"
    pos2 = {"entry_price": 100, "stop": 94, "highest": 100, "atr": 3}
    assert strategies.update_exit(pos2, 93, 1, cfg)[2] == "stop-loss"
    assert strategies.update_exit(pos2, 101, 15, cfg)[2] == "max. doba držení"


def test_sizing_respects_risk_and_caps(cfg):
    # 7000 active, 2.5% risk = 175; risk/share 10 -> 17.5 shares, cap 30% = 2100 -> 21 shares
    assert size_active(7000, 7000, 100, 90, 1.0, cfg) == 17.5
    # tight stop -> capped by max position size
    assert size_active(7000, 7000, 100, 99, 1.0, cfg) == 21
    # little cash -> capped by available
    assert size_active(7000, 500, 100, 90, 1.0, cfg) == 5
    # below min order value -> skip
    assert size_active(7000, 5, 100, 90, 1.0, cfg) == 0


def test_guards(cfg):
    assert check_guards(1000, 1000, 1000, False, cfg).allow_entries
    g = check_guards(930, 1000, 1000, False, cfg)
    assert not g.allow_entries and not g.halted
    assert check_guards(640, 640, 1000, False, cfg).halted
    assert check_guards(1000, 1000, 1000, True, cfg).halted


def test_core_buys_more_in_dip(cfg):
    stats_dip = {"IE00BFMXXD54": (85, 100), "IE00BK5BQT80": (98, 100)}
    buys = strategies.core_buys(3000, {}, stats_dip, cfg)
    assert buys["IE00BFMXXD54"] == pytest.approx(1800)      # full shortfall in dip
    assert buys["IE00BK5BQT80"] == pytest.approx(300)       # 25% of 1200 otherwise
    assert strategies.core_buys(3000, {"IE00BFMXXD54": 1800, "IE00BK5BQT80": 1200},
                                stats_dip, cfg) == {}


# ---------------------------------------------------------------- engine flows
def breakout_setup(cfg):
    up = make_df(0.004)
    entry = float(up["High"].iloc[-20:].max()) * 1.01
    broker = FakeBroker()
    broker.prices["UPPY_US_EQ"] = entry
    data = FakeData({"UPPY": up, "DOWNY": make_df(-0.003)}, {"UPPY": entry, "DOWNY": 50.0})
    return broker, data, entry


def test_entry_places_broker_stop_then_exit_cancels_it(cfg):
    cfg["core"]["run_weekday"] = 4  # keep core out of this test
    broker, data, entry = breakout_setup(cfg)
    eng, tg = make_engine(cfg, broker, data)
    eng.cycle()
    assert broker.orders and broker.orders[0][0] == "UPPY_US_EQ" and broker.orders[0][1] > 0
    pos = eng.state.position("UPPY_US_EQ")
    assert pos and pos["broker_stop_id"] in broker.stops
    assert any("NÁKUP UPPY" in m for m in tg.sent)

    # second cycle at the same price must not buy again
    eng.cycle()
    assert len(broker.orders) == 1

    # price collapses below stop -> sell everything, cancel broker stop
    crash = pos["stop"] * 0.98
    data.prices["UPPY"] = crash
    broker.prices["UPPY_US_EQ"] = crash
    eng.cycle()
    assert "UPPY_US_EQ" not in broker.pos
    assert not broker.stops
    assert eng.state.position("UPPY_US_EQ") is None
    assert any("PRODEJ UPPY" in m and "stop-loss" in m for m in tg.sent)


def test_uncertain_order_is_not_retried_then_adopted(cfg):
    cfg["core"]["run_weekday"] = 4
    broker, data, entry = breakout_setup(cfg)
    broker.fail_next_order = OrderUncertain("timeout")
    eng, tg = make_engine(cfg, broker, data)
    eng.cycle()
    assert broker.orders == []
    assert any("nejisté" in m for m in tg.sent)
    eng.cycle()                               # intent blocks a duplicate buy
    assert broker.orders == []
    # the order actually went through at the broker after all
    broker.pos["UPPY_US_EQ"] = {"qty": 10.0, "avg": entry}
    eng.cycle()
    pos = eng.state.position("UPPY_US_EQ")
    assert pos and pos["quantity"] == 10 and pos["broker_stop_id"] in broker.stops


def test_broker_stop_fired_is_reconciled(cfg):
    cfg["core"]["run_weekday"] = 4
    broker, data, entry = breakout_setup(cfg)
    eng, tg = make_engine(cfg, broker, data)
    eng.cycle()
    del broker.pos["UPPY_US_EQ"]             # stop executed at the broker overnight
    eng.cycle()
    assert eng.state.position("UPPY_US_EQ") is None
    assert any("stop příkazem u brokera" in m for m in tg.sent)


def test_daily_loss_cap_blocks_new_entries(cfg):
    cfg["core"]["run_weekday"] = 4
    broker, data, entry = breakout_setup(cfg)
    eng, tg = make_engine(cfg, broker, data)
    eng.state.log_equity("2026-11-23", 20000)  # day started much higher
    eng.cycle()
    assert broker.orders == []
    assert any("Pojistka" in m for m in tg.sent)


def test_stop_command_and_flatten(cfg):
    cfg["core"]["run_weekday"] = 4
    broker, data, entry = breakout_setup(cfg)
    eng, tg = make_engine(cfg, broker, data)
    eng.cycle()
    assert "UPPY_US_EQ" in broker.pos
    tg.queue = ["/flatten"]
    eng.cycle()
    assert "UPPY_US_EQ" not in broker.pos and eng.state.get("manual_stop")
    eng.cycle()
    assert len([o for o in broker.orders if o[1] > 0]) == 1   # no new buys while stopped


def test_core_runs_once_per_week_on_monday(cfg):
    cfg["markets"]["us"]["universe"] = []
    broker = FakeBroker()
    etf = make_df(0.0005, start=100)
    px = float(etf["High"].iloc[-252:].max()) * 0.85         # in a 15% dip
    data = FakeData({"VUAA.L": etf, "VWCE.DE": etf}, {"VUAA.L": px, "VWCE.DE": px})
    broker.prices.update({"VUAAl_EQ": px, "VWCEd_EQ": px})
    eng, tg = make_engine(cfg, broker, data)
    eng.cycle()
    bought = {t for t, q in broker.orders}
    assert bought == {"VUAAl_EQ", "VWCEd_EQ"}
    n = len(broker.orders)
    eng.cycle()
    assert len(broker.orders) == n            # not again this week


def test_dry_run_sends_no_orders(cfg):
    cfg["dry_run"] = True
    cfg["core"]["run_weekday"] = 4
    broker, data, entry = breakout_setup(cfg)
    eng, tg = make_engine(cfg, broker, data)
    eng.cycle()
    assert broker.orders == [] and any("DRY RUN" in m for m in tg.sent)


def test_aggressive_config_loads_and_trades(tmp_path, monkeypatch):
    from tradebot.config import load_config
    monkeypatch.setenv("BOT_CONFIG", "config-aggressive.yaml")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    c = load_config()
    assert c["allocation"]["core_pct"] == 0 and c["risk"]["max_positions"] == 3
    c["markets"]["us"]["universe"] = ["UPPY"]
    c["markets"]["eu"]["enabled"] = False
    up = make_df(0.006, vol=0.02, seed=3)
    up["High"], up["Low"] = up["Close"] * 1.025, up["Close"] * 0.975
    entry = float(up["High"].iloc[-10:].max()) * 1.01
    broker = FakeBroker()
    broker.prices["UPPY_US_EQ"] = entry
    eng, tg = make_engine(c, broker, FakeData({"UPPY": up}, {"UPPY": entry}))
    eng.cycle()
    assert broker.orders and broker.orders[0][0] == "UPPY_US_EQ"


def test_telegram_offset_persists_between_runs(tmp_path, monkeypatch):
    from tradebot import notify
    st = State(tmp_path / "s.sqlite")

    class R:
        def __init__(self, data): self.data = data
        def json(self): return self.data

    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["offset"])
        if params["offset"] <= 5:
            return R({"result": [{"update_id": 5, "message": {"chat": {"id": 42}, "text": "/stop"}}]})
        return R({"result": []})

    monkeypatch.setattr(notify.requests, "get", fake_get)
    assert notify.Telegram("t", "42", st).commands() == ["/stop"]
    assert notify.Telegram("t", "42", st).commands() == []   # new run, already acknowledged
    assert calls == [0, 6]


def test_eu_market_trades_with_t212_suffix_and_reports_missing(cfg):
    cfg["markets"]["eu"]["enabled"] = True
    cfg["markets"]["eu"]["universe"] = ["RHM.DE", "NOPE.PA"]
    cfg["core"]["run_weekday"] = 4
    up = make_df(0.004)
    entry = float(up["High"].iloc[-20:].max()) * 1.01
    broker = FakeBroker()
    broker.inst.append({"ticker": "RHMd_EQ", "type": "STOCK", "currencyCode": "EUR",
                        "isin": "DE0007030033", "shortName": "RHM"})
    broker.prices["RHMd_EQ"] = entry
    data = FakeData({"RHM.DE": up, "UPPY": make_df(-0.003)}, {"RHM.DE": entry, "UPPY": 50.0})
    berlin_morning = datetime(2026, 11, 27, 11, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    eng, tg = make_engine(cfg, broker, data, when=berlin_morning)
    eng.market_open = lambda m="us": m == "eu"
    eng.cycle()
    assert broker.orders and broker.orders[0][0] == "RHMd_EQ"
    assert any("Trh EU" in m and "NOPE.PA" in m for m in tg.sent)
    pos = eng.state.position("RHMd_EQ")
    assert pos["symbol"] == "RHM.DE"
    # EU closed, US open: EU position is not touched by the exit logic
    data.prices["RHM.DE"] = pos["stop"] * 0.5
    eng.market_open = lambda m="us": m == "us"
    eng.cycle()
    assert eng.state.position("RHMd_EQ") is not None
