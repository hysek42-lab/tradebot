"""Orchestration: one `cycle()` = reconcile, protect, exit, enter, report."""
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import strategies
from .risk import check_guards, round_down, size_active
from .t212 import OrderUncertain, T212Error

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
PRAGUE = ZoneInfo("Europe/Prague")


class Engine:
    def __init__(self, cfg, client, data, state, notifier, clock=None, sleep=time.sleep):
        self.cfg = cfg
        self.client = client
        self.data = data
        self.state = state
        self.tg = notifier
        self.clock = clock or (lambda: datetime.now(tz=NY))
        self.sleep = sleep
        self._instruments: dict | None = None
        self._last_scan: datetime | None = None
        self.account_ccy = "EUR"

    # ------------------------------------------------------------------ utils
    def _now(self) -> datetime:
        return self.clock().astimezone(NY)

    def _dry(self) -> bool:
        return bool(self.cfg.get("dry_run"))

    def instruments(self) -> dict:
        """Map of t212 ticker -> instrument, cached on disk for a day."""
        if self._instruments is not None:
            return self._instruments
        cache = self.cfg["data_dir"] / "instruments.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
            items = json.loads(cache.read_text())
        else:
            items = self.client.instruments()
            cache.write_text(json.dumps(items))
        self._instruments = {i["ticker"]: i for i in items}
        return self._instruments

    def us_ticker(self, symbol: str) -> str | None:
        t = f"{symbol.replace('-', '.')}_US_EQ"
        return t if t in self.instruments() else None

    def etf_ticker(self, isin: str) -> str | None:
        matches = [i for i in self.instruments().values()
                   if i.get("isin") == isin and i.get("type") == "ETF"]
        if not matches:
            return None
        matches.sort(key=lambda i: i.get("currencyCode") != self.account_ccy)
        return matches[0]["ticker"]

    def market_open(self) -> bool:
        now = self._now()
        if now.weekday() >= 5:
            return False
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if not open_t <= now < close_t:
            return False
        # holiday check: fresh SPY bar means the market is actually trading
        spy = self.data.last_prices(["SPY"]).get("SPY")
        return bool(spy and now - spy[1].astimezone(NY) < timedelta(minutes=30))

    def minutes_since_open(self) -> float:
        now = self._now()
        return (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60

    # --------------------------------------------------------------- commands
    def handle_commands(self, total: float | None = None) -> None:
        for cmd in self.tg.commands():
            if cmd == "/stop":
                self.state.set("manual_stop", True)
                self.tg.send("Bot zastaven. Otevřené pozice drží dál se stop-lossy. /start pro obnovení.")
            elif cmd == "/start":
                self.state.set("manual_stop", False)
                self.state.set("peak_reset_ts", time.time())
                self.tg.send("Bot znovu běží.")
            elif cmd == "/flatten":
                self.state.set("manual_stop", True)
                self.state.set("flatten_requested", True)
                self.tg.send("Zavírám všechny aktivní pozice a zastavuji bota.")
            elif cmd == "/status":
                self.tg.send(self.status_text())

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> None:
        self.handle_commands()
        summary = self.client.summary()
        self.account_ccy = summary.get("currency", self.account_ccy)
        total = float(summary["totalValue"])
        cash = float(summary["cash"]["availableToTrade"])
        day = self._now().date().isoformat()
        self.state.log_equity(day, total)

        guard = check_guards(total, self.state.day_start_equity(day),
                             self.state.peak_equity(self.state.get("peak_reset_ts", 0)),
                             bool(self.state.get("manual_stop")), self.cfg)
        if guard.reason and self.state.get("last_guard_reason") != guard.reason:
            self.tg.send(f"Pojistka: {guard.reason}")
        self.state.set("last_guard_reason", guard.reason)

        broker = {p["instrument"]["ticker"]: p for p in self.client.positions()}
        self.reconcile(broker)
        self.maybe_report(total)

        if not self.market_open():
            return

        if self.state.get("flatten_requested"):
            for pos in self.state.positions("active"):
                self.exit_position(pos, broker, "ruční /flatten")
            self.state.set("flatten_requested", False)
            return

        self.manage_exits(broker)

        if not guard.allow_entries:
            return
        a = self.cfg["active"]
        scan_due = (self._last_scan is None or
                    self._now() - self._last_scan >= timedelta(minutes=a["scan_every_minutes"] - 1))
        if scan_due and self.minutes_since_open() >= a["skip_first_minutes_after_open"]:
            self._last_scan = self._now()
            self.scan_entries(total, cash, broker)
        self.maybe_core(total, cash, broker)

    # -------------------------------------------------------------- reconcile
    def reconcile(self, broker: dict) -> None:
        for pos in self.state.positions("active"):
            bp = broker.get(pos["ticker"])
            if bp is None or bp["quantity"] <= 0:
                # closed outside the bot's exit logic (usually the broker stop fired)
                pnl = (pos["stop"] - pos["entry_price"]) * pos["quantity"]
                self.state.log_trade(pos["ticker"], "SELL", pos["quantity"], pos["stop"],
                                     "active", "stop u brokera", pnl)
                self.state.remove_position(pos["ticker"])
                self.tg.send(f"{pos['symbol']}: pozice zavřena stop příkazem u brokera "
                             f"(~{pos['stop']:.2f}).")
            elif abs(bp["quantity"] - pos["quantity"]) > 1e-9:
                pos["quantity"] = bp["quantity"]
                self.state.upsert_position(pos)
        # adopt fills from orders whose confirmation got lost (intent recorded, no state yet)
        for ticker, bp in broker.items():
            intent = self.state.get_intent(ticker)
            if (self.state.position(ticker) is None and intent and intent.startswith("active:")
                    and self.state.has_recent_intent(ticker, 86400)):
                symbol = ticker.split("_")[0]
                daily = self.data.daily([symbol]).get(symbol)
                if daily is None:
                    continue
                from .indicators import atr as atr_fn
                a_now = float(atr_fn(daily, self.cfg["active"]["atr_days"]).iloc[-1])
                entry = float(bp["averagePricePaid"])
                self._open_state(ticker, symbol, float(bp["quantity"]), entry, a_now,
                                 entry - self.cfg["active"]["stop_atr_mult"] * a_now)
                self.tg.send(f"{symbol}: dohledán nepotvrzený nákup, pozice převzata.")

    # ------------------------------------------------------------------ exits
    def manage_exits(self, broker: dict) -> None:
        active = self.state.positions("active")
        if not active:
            return
        prices = self.data.last_prices([p["symbol"] for p in active])
        for pos in active:
            px = prices.get(pos["symbol"])
            if not px:
                continue
            days = (time.time() - pos["opened_at"]) / 86400 * (5 / 7)  # ~trading days
            stop, highest, reason = strategies.update_exit(pos, px[0], days, self.cfg)
            pos["stop"], pos["highest"] = stop, highest
            self.state.upsert_position(pos)
            if reason:
                self.exit_position(pos, broker, reason, px[0])

    def exit_position(self, pos: dict, broker: dict, reason: str, price: float | None = None) -> None:
        bp = broker.get(pos["ticker"])
        qty = float(bp["quantityAvailableForTrading"]) if bp else pos["quantity"]
        if pos.get("broker_stop_id") and not self._dry():
            self.client.cancel_order(pos["broker_stop_id"])
            self.sleep(1.5)
            qty = pos["quantity"]
        price = price or (float(bp["currentPrice"]) if bp else pos["entry_price"])
        if not self._dry():
            try:
                self.client.market_order(pos["ticker"], -round_down(qty, 6))
            except OrderUncertain as e:
                self.tg.send(f"{pos['symbol']}: nejisté odeslání prodeje, ověřím příště. {e}")
                return
            except T212Error as e:
                self.tg.send(f"{pos['symbol']}: prodej odmítnut ({e}). Stop u brokera zůstává.")
                if pos.get("broker_stop_id"):
                    self._place_stop(pos)
                return
        pnl = (price - pos["entry_price"]) * qty
        self.state.log_trade(pos["ticker"], "SELL", qty, price, "active", reason, pnl)
        self.state.remove_position(pos["ticker"])
        self.tg.send(f"PRODEJ {pos['symbol']} {qty} ks @ {price:.2f} ({reason}), "
                     f"výsledek {pnl:+.2f} {self._inst_ccy(pos['ticker'])}")

    # ----------------------------------------------------------------- entries
    def scan_entries(self, total: float, cash: float, broker: dict) -> None:
        a, r = self.cfg["active"], self.cfg["risk"]
        held = self.state.positions("active")
        if len(held) >= r["max_positions"]:
            return
        universe = [s for s in a["universe"]
                    if self.us_ticker(s) and self.state.position(self.us_ticker(s)) is None]
        daily = self.data.daily(universe)
        prices = {s: p for s, (p, _) in self.data.last_prices(list(daily)).items()}
        candidates = strategies.active_candidates(daily, prices, self.cfg)
        if not candidates:
            return

        active_capital = total * self.cfg["allocation"]["active_pct"]
        exposure = sum(float(broker[p["ticker"]]["walletImpact"]["currentValue"])
                       for p in held if p["ticker"] in broker)
        spendable = cash - total * self.cfg["allocation"]["cash_buffer_pct"]
        fx = self.data.fx("USD", self.account_ccy)

        for c in candidates[: r["max_positions"] - len(held)]:
            ticker = self.us_ticker(c.symbol)
            if self.state.has_recent_intent(ticker):
                continue
            available = min(active_capital - exposure, spendable)
            qty = size_active(active_capital, available, c.price, c.stop, fx, self.cfg)
            if qty <= 0:
                continue
            if self._buy(ticker, c.symbol, qty, c.price, c.atr, c.stop, "active", "breakout"):
                exposure += qty * c.price * fx
                spendable -= qty * c.price * fx

    def _buy(self, ticker, symbol, qty, price, atr_v, stop, bucket, reason) -> bool:
        if self._dry():
            self.tg.send(f"[DRY RUN] NÁKUP {symbol} {qty} ks @ ~{price:.2f}, stop {stop:.2f}")
            return True
        before = {p["instrument"]["ticker"]: p for p in self.client.positions()}
        prev_qty = float(before[ticker]["quantity"]) if ticker in before else 0.0
        self.state.add_intent(ticker, f"{bucket}:BUY {qty} {reason}")
        try:
            self.client.market_order(ticker, qty)
        except OrderUncertain as e:
            self.tg.send(f"{symbol}: nejisté odeslání nákupu, nebudu opakovat, ověřím příště. {e}")
            return False
        except T212Error as e:
            self.state.clear_intent(ticker)
            self.tg.send(f"{symbol}: nákup odmítnut ({e}).")
            return False

        for _ in range(6):  # wait for the fill
            self.sleep(3)
            now = {p["instrument"]["ticker"]: p for p in self.client.positions()}
            if ticker in now and float(now[ticker]["quantity"]) > prev_qty + 1e-9:
                filled = float(now[ticker]["quantity"]) - prev_qty
                fill_px = float(now[ticker]["currentPrice"]) if prev_qty else \
                    float(now[ticker]["averagePricePaid"])
                break
        else:
            self.tg.send(f"{symbol}: příkaz odeslán, plnění zatím nepotvrzeno. Ověřím příště.")
            return False

        self.state.log_trade(ticker, "BUY", filled, fill_px, bucket, reason)
        self.state.clear_intent(ticker)
        if bucket == "active":
            stop = fill_px - (price - stop)  # keep the ATR distance relative to actual fill
            self._open_state(ticker, symbol, filled, fill_px, atr_v, stop)
            self.tg.send(f"NÁKUP {symbol} {filled} ks @ {fill_px:.2f}, stop-loss {stop:.2f}")
        else:
            self.tg.send(f"NÁKUP (dlouhodobě) {symbol} {filled} ks @ {fill_px:.2f}")
        return True

    def _open_state(self, ticker, symbol, qty, entry, atr_v, stop) -> None:
        pos = {"ticker": ticker, "symbol": symbol, "bucket": "active", "quantity": qty,
               "entry_price": entry, "stop": stop, "highest": entry, "atr": atr_v,
               "broker_stop_id": None, "opened_at": time.time()}
        self._place_stop(pos)
        self.state.upsert_position(pos)
        self.state.clear_intent(ticker)

    def _place_stop(self, pos: dict) -> None:
        """Protective GTC stop at the broker, so losses are capped even if the bot is down."""
        try:
            order = self.client.stop_order(pos["ticker"], -round_down(pos["quantity"], 6),
                                           round(pos["stop"], 2))
            pos["broker_stop_id"] = order.get("id") if order else None
        except (T212Error, OrderUncertain) as e:
            pos["broker_stop_id"] = None
            self.tg.send(f"{pos['symbol']}: stop u brokera se nepodařilo zadat ({e}). "
                         f"Hlídám stop softwarově.")

    # -------------------------------------------------------------------- core
    def maybe_core(self, total: float, cash: float, broker: dict) -> None:
        c = self.cfg["core"]
        now = self._now()
        week = now.strftime("%G-W%V")
        if now.weekday() != c["run_weekday"] or self.state.get("core_last_week") == week:
            return
        if self.minutes_since_open() < 60:
            return
        self.state.set("core_last_week", week)

        etfs = {e["isin"]: e for e in c["etfs"]}
        held = {}
        for p in broker.values():
            isin = p["instrument"].get("isin")
            if isin in etfs:
                held[isin] = held.get(isin, 0) + float(p["walletImpact"]["currentValue"])
        daily = self.data.daily([e["yahoo"] for e in etfs.values()])
        prices = self.data.last_prices([e["yahoo"] for e in etfs.values()])
        stats = {}
        for isin, e in etfs.items():
            df, px = daily.get(e["yahoo"]), prices.get(e["yahoo"])
            if df is not None and px:
                stats[isin] = (px[0], float(df["High"].iloc[-252:].max()))

        core_capital = total * self.cfg["allocation"]["core_pct"]
        spendable = cash - total * self.cfg["allocation"]["cash_buffer_pct"]
        for isin, value in strategies.core_buys(core_capital, held, stats, self.cfg).items():
            e, ticker = etfs[isin], self.etf_ticker(isin)
            if not ticker or value > spendable:
                continue
            px = stats[isin][0]
            fx = self.data.fx(self.data.currency(e["yahoo"]), self.account_ccy)
            qty = round_down(value / (px * fx), self.cfg["order"]["qty_decimals"])
            if qty > 0 and self._buy(ticker, e["name"], qty, px, 0, 0, "core", "ETF nákup"):
                spendable -= value

    # ----------------------------------------------------------------- reports
    def _inst_ccy(self, ticker: str) -> str:
        return (self._instruments or {}).get(ticker, {}).get("currencyCode", "")

    def status_text(self) -> str:
        s = self.client.summary()
        lines = [f"Hodnota účtu: {float(s['totalValue']):.2f} {s.get('currency', '')}",
                 f"Volná hotovost: {float(s['cash']['availableToTrade']):.2f}",
                 f"Bot: {'ZASTAVEN' if self.state.get('manual_stop') else 'běží'} "
                 f"({self.cfg['environment']}{', dry run' if self._dry() else ''})"]
        for p in self.state.positions("active"):
            lines.append(f"- {p['symbol']}: {p['quantity']} ks, vstup {p['entry_price']:.2f}, "
                         f"stop {p['stop']:.2f}")
        return "\n".join(lines)

    def maybe_report(self, total: float) -> None:
        now_p = self._now().astimezone(PRAGUE)
        hh, mm = map(int, self.cfg["report_time"].split(":"))
        today = now_p.date().isoformat()
        if (now_p.hour, now_p.minute) < (hh, mm) or self.state.get("last_report_day") == today:
            return
        self.state.set("last_report_day", today)
        start = self.state.day_start_equity(self._now().date().isoformat()) or total
        midnight = now_p.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        trades = self.state.trades_since(midnight)
        lines = [f"Denní report {today}",
                 f"Hodnota: {total:.2f} {self.account_ccy} ({total - start:+.2f} dnes)",
                 f"Obchody dnes: {len(trades)}"]
        lines += [f"- {t['side']} {t['ticker']} {t['quantity']} @ {t['price']:.2f} ({t['reason']})"
                  for t in trades]
        self.tg.send("\n".join(lines) + "\n\n" + self.status_text())
