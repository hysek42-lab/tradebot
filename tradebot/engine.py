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

    # ---------------------------------------------------------------- markets
    def markets(self) -> dict:
        m = self.cfg.get("markets") or {
            "us": {"tz": "America/New_York", "open": "09:30", "close": "16:00",
                   "ref": "SPY", "currency": "USD",
                   "universe": self.cfg["active"].get("universe", [])}}
        return {k: v for k, v in m.items() if v.get("enabled", True)}

    def market_of(self, symbol: str) -> str:
        for name, m in (self.cfg.get("markets") or {}).items():
            if symbol in m.get("universe", []):
                return name
        return "eu" if "." in symbol else "us"

    def ticker_for(self, symbol: str, market: str) -> str | None:
        inst = self.instruments()
        if market == "us":
            t = f"{symbol.replace('-', '.')}_US_EQ"
            return t if t in inst else None
        m = self.markets().get(market) or (self.cfg.get("markets") or {}).get(market, {})
        base, _, suffix = symbol.partition(".")
        hint = (m.get("t212_suffix") or {}).get("." + suffix, "")
        t = f"{base}{hint}_EQ"
        if t in inst:
            return t
        for i in inst.values():  # fallback: same short name and currency
            if (i.get("type") == "STOCK" and i.get("shortName") == base
                    and i.get("currencyCode") == m.get("currency")):
                return i["ticker"]
        return None

    def us_ticker(self, symbol: str) -> str | None:
        return self.ticker_for(symbol, "us")

    def etf_ticker(self, isin: str) -> str | None:
        matches = [i for i in self.instruments().values()
                   if i.get("isin") == isin and i.get("type") == "ETF"]
        if not matches:
            return None
        matches.sort(key=lambda i: i.get("currencyCode") != self.account_ccy)
        return matches[0]["ticker"]

    def _session(self, market: str):
        m = self.markets()[market]
        tz = ZoneInfo(m["tz"])
        now = self.clock().astimezone(tz)
        oh, om = map(int, m["open"].split(":"))
        ch, cm = map(int, m["close"].split(":"))
        return (now, now.replace(hour=oh, minute=om, second=0, microsecond=0),
                now.replace(hour=ch, minute=cm, second=0, microsecond=0), m)

    def market_open(self, market: str = "us") -> bool:
        now, open_t, close_t, m = self._session(market)
        if now.weekday() >= 5 or not open_t <= now < close_t:
            return False
        # holiday check: a fresh bar of the reference symbol means it is really trading
        ref = self.data.last_prices([m["ref"]]).get(m["ref"])
        return bool(ref and now - ref[1].astimezone(now.tzinfo) < timedelta(minutes=30))

    def minutes_since_open(self, market: str = "us") -> float:
        now, open_t, _, _ = self._session(market)
        return (now - open_t).total_seconds() / 60

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
        by_ccy = self.cfg["risk"].get("min_order_value_by_ccy") or {}
        if self.account_ccy in by_ccy:
            self.cfg["risk"]["min_order_value"] = by_ccy[self.account_ccy]
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

        open_markets = [m for m in self.markets() if self.market_open(m)]
        self.state.set("diag_markets", {"ts": time.time(), "open": open_markets})
        if not open_markets:
            return

        if self.state.get("flatten_requested"):
            remaining = 0
            for pos in self.state.positions("active"):
                if self.market_of(pos["symbol"]) in open_markets:
                    self.exit_position(pos, broker, "ruční /flatten")
                else:
                    remaining += 1
            self.state.set("flatten_requested", remaining > 0)
            return

        self.manage_exits(broker, open_markets)
        self.manage_dip_exits(broker, open_markets)

        if not guard.allow_entries:
            return
        skip = self.cfg["active"]["skip_first_minutes_after_open"]
        for m in open_markets:
            if self.minutes_since_open(m) >= skip:
                cash -= self.scan_entries(total, cash, broker, m)
                cash -= self.scan_dips(total, cash, broker, m)
        if "us" in open_markets:
            self.maybe_core(total, cash, broker)

    # -------------------------------------------------------------- reconcile
    def reconcile(self, broker: dict) -> None:
        for pos in self.state.positions("dip"):
            bp = broker.get(pos["ticker"])
            if bp is None or bp["quantity"] <= 0:
                self.state.remove_position(pos["ticker"])
                self.tg.send(f"{pos['symbol']}: pozice z poklesové strategie zmizela z účtu "
                             f"(prodaná ručně?). Přestávám ji sledovat.")
            elif abs(bp["quantity"] - pos["quantity"]) > 1e-9:
                pos["quantity"] = bp["quantity"]
                self.state.upsert_position(pos)
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
            if (self.state.position(ticker) is None and intent and intent.startswith("dip:")
                    and self.state.has_recent_intent(ticker, 86400)):
                self._open_dip_state(ticker, intent.split(":")[1], float(bp["quantity"]),
                                     float(bp["averagePricePaid"]))
                self.tg.send(f"{intent.split(':')[1]}: dohledán nepotvrzený nákup, pozice převzata.")
                continue
            if (self.state.position(ticker) is None and intent and intent.startswith("active:")
                    and self.state.has_recent_intent(ticker, 86400)):
                parts = intent.split(":")
                symbol = parts[1] if len(parts) > 2 else ticker.split("_")[0]
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
    def manage_exits(self, broker: dict, open_markets=("us",)) -> None:
        active = [p for p in self.state.positions("active")
                  if self.market_of(p["symbol"]) in open_markets]
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
                self.tg.send(f"{pos['symbol']}: prodej odmítnut ({e}).")
                if pos.get("broker_stop_id"):
                    self._place_stop(pos)
                return
        pnl = (price - pos["entry_price"]) * qty
        self.state.log_trade(pos["ticker"], "SELL", qty, price, pos["bucket"], reason, pnl)
        if pos["bucket"] == "dip":  # no immediate re-buy of the same stock
            self.state.set(f"dip_cooldown_{pos['ticker']}", time.time())
        self.state.remove_position(pos["ticker"])
        self.tg.send(f"PRODEJ {pos['symbol']} {qty} ks @ {price:.2f} ({reason}), "
                     f"výsledek {pnl:+.2f} {self._inst_ccy(pos['ticker'])}")

    # ----------------------------------------------------------------- entries
    def scan_entries(self, total: float, cash: float, broker: dict, market: str = "us") -> float:
        """Look for entries on one market. Returns cash spent (account currency)."""
        r = self.cfg["risk"]
        m = self.markets()[market]
        held = self.state.positions("active")
        if len(held) >= r["max_positions"]:
            return 0.0
        acfg = {**self.cfg, "active": {**self.cfg["active"], **(m.get("overrides") or {})}}

        tickers = {s: self.ticker_for(s, market) for s in m.get("universe", [])}
        self._report_unmatched(market, tickers)
        universe = [s for s, t in tickers.items() if t and self.state.position(t) is None]
        daily = self.data.daily(universe)
        prices = {s: p for s, (p, _) in self.data.last_prices(list(daily)).items()}
        candidates = strategies.active_candidates(daily, prices, acfg)
        self.state.set(f"diag_scan_{market}", {
            "ts": time.time(), "titles": len(tickers), "matched": len(universe),
            "daily": len(daily), "prices": len(prices),
            "candidates": [c.symbol for c in candidates[:5]]})
        if not candidates:
            return 0.0

        active_capital = total * self.cfg["allocation"]["active_pct"]
        exposure = sum(float(broker[p["ticker"]]["walletImpact"]["currentValue"])
                       for p in held if p["ticker"] in broker)
        spendable = cash - total * self.cfg["allocation"]["cash_buffer_pct"]
        fx = self.data.fx(m["currency"], self.account_ccy)
        spent = 0.0

        for c in candidates[: r["max_positions"] - len(held)]:
            ticker = tickers[c.symbol]
            if self.state.has_recent_intent(ticker):
                continue
            available = min(active_capital - exposure, spendable - spent)
            qty = size_active(active_capital, available, c.price, c.stop, fx, self.cfg)
            if qty <= 0:
                continue
            if self._buy(ticker, c.symbol, qty, c.price, c.atr, c.stop, "active", c.kind):
                exposure += qty * c.price * fx
                spent += qty * c.price * fx
        return spent

    def _report_unmatched(self, market: str, tickers: dict) -> None:
        key = f"universe_check_{market}"
        today = self.clock().date().isoformat()
        if self.state.get(key) == today:
            return
        self.state.set(key, today)
        missing = [s for s, t in tickers.items() if not t]
        if missing:
            self.tg.send(f"Trh {market.upper()}: {len(tickers) - len(missing)}/{len(tickers)} titulů "
                         f"nalezeno v Trading 212. Chybí: {', '.join(missing)}")

    def _buy(self, ticker, symbol, qty, price, atr_v, stop, bucket, reason) -> bool:
        if self._dry():
            self.tg.send(f"[DRY RUN] NÁKUP {symbol} {qty} ks @ ~{price:.2f}, stop {stop:.2f}")
            return True
        before = {p["instrument"]["ticker"]: p for p in self.client.positions()}
        prev_qty = float(before[ticker]["quantity"]) if ticker in before else 0.0
        self.state.add_intent(ticker, f"{bucket}:{symbol}:BUY {qty} {reason}")
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
        if bucket == "dip":
            self._open_dip_state(ticker, symbol, filled, fill_px)
            tp = self.cfg["dip"]["take_profit_pct"]
            self.tg.send(f"NÁKUP (pokles) {symbol} {filled} ks @ {fill_px:.2f}, "
                         f"prodej při {fill_px * (1 + tp):.2f} (+{tp:.0%}), bez stop-lossu")
        elif bucket == "active":
            stop = fill_px - (price - stop)  # keep the ATR distance relative to actual fill
            self._open_state(ticker, symbol, filled, fill_px, atr_v, stop)
            why = "prudký růst dnes" if reason == "spike" else "proražení maxima"
            self.tg.send(f"NÁKUP {symbol} {filled} ks @ {fill_px:.2f} ({why}), stop-loss {stop:.2f}")
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

    # ------------------------------------------------------------- dip buying
    def _open_dip_state(self, ticker, symbol, qty, entry) -> None:
        self.state.upsert_position({
            "ticker": ticker, "symbol": symbol, "bucket": "dip", "quantity": qty,
            "entry_price": entry, "stop": None, "highest": entry, "atr": 0.0,
            "broker_stop_id": None, "opened_at": time.time()})
        self.state.clear_intent(ticker)

    def manage_dip_exits(self, broker: dict, open_markets) -> None:
        d = self.cfg.get("dip") or {}
        held = [p for p in self.state.positions("dip") if self.market_of(p["symbol"]) in open_markets]
        if not d.get("enabled") or not held:
            return
        prices = self.data.last_prices([p["symbol"] for p in held])
        for pos in held:
            px = prices.get(pos["symbol"])
            reason = strategies.dip_exit(pos, px[0], d) if px else None
            if reason:
                self.exit_position(pos, broker, reason, px[0])

    def scan_dips(self, total: float, cash: float, broker: dict, market: str) -> float:
        d = self.cfg.get("dip") or {}
        universe_all = (d.get("universe") or {}).get(market, [])
        held = self.state.positions("dip")
        if not d.get("enabled") or not universe_all or len(held) >= d["max_positions"]:
            return 0.0
        m = self.markets()[market]
        tickers = {s: self.ticker_for(s, market) for s in universe_all}
        cooldown = d.get("cooldown_hours", 24) * 3600
        universe = [s for s, t in tickers.items() if t and self.state.position(t) is None
                    and time.time() - self.state.get(f"dip_cooldown_{t}", 0) > cooldown]
        daily = self.data.daily(universe)
        prices = {s: p for s, (p, _) in self.data.last_prices(list(daily)).items()}
        candidates = strategies.dip_candidates(daily, prices, d)
        self.state.set(f"diag_dip_{market}", {"ts": time.time(), "titles": len(universe_all),
                                              "candidates": [c.symbol for c in candidates[:5]]})
        dip_capital = total * self.cfg["allocation"].get("dip_pct", 0)
        exposure = sum(float(broker[p["ticker"]]["walletImpact"]["currentValue"])
                       for p in held if p["ticker"] in broker)
        per_position = dip_capital / d["max_positions"]
        spendable = cash - total * self.cfg["allocation"]["cash_buffer_pct"]
        fx = self.data.fx(m["currency"], self.account_ccy)
        spent = 0.0
        for c in candidates[: d["max_positions"] - len(held)]:
            ticker = tickers[c.symbol]
            if self.state.has_recent_intent(ticker):
                continue
            value = min(per_position, dip_capital - exposure, spendable - spent)
            qty = round_down(value / (c.price * fx), self.cfg["order"]["qty_decimals"])
            if qty <= 0 or qty * c.price * fx < self.cfg["risk"]["min_order_value"]:
                continue
            if self._buy(ticker, c.symbol, qty, c.price, 0, 0, "dip", "dip"):
                exposure += qty * c.price * fx
                spent += qty * c.price * fx
        return spent

    # -------------------------------------------------------------------- core
    def maybe_core(self, total: float, cash: float, broker: dict) -> None:
        c = self.cfg["core"]
        now = self._now()
        week = now.strftime("%G-W%V")
        last = self.state.get("core_last_week")
        if last == week or (last is not None and now.weekday() != c["run_weekday"]):
            return  # first ever run happens on any weekday, then weekly
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
        diag = self.state.get("diag_markets")
        if diag:
            ago = (time.time() - diag["ts"]) / 60
            lines.append(f"Poslední běh před {ago:.0f} min, otevřené trhy: "
                         f"{', '.join(m.upper() for m in diag['open']) or 'žádný'}")
        for name in self.markets():
            d = self.state.get(f"diag_scan_{name}")
            if d:
                lines.append(f"Sken {name.upper()} před {(time.time() - d['ts']) / 60:.0f} min: "
                             f"{d['matched']}/{d['titles']} titulů, data {d['daily']}, ceny {d['prices']}, "
                             f"signály: {', '.join(d['candidates']) or 'žádné'}")
        for name in self.markets():
            d = self.state.get(f"diag_dip_{name}")
            if d:
                lines.append(f"Poklesy {name.upper()}: kandidáti {', '.join(d['candidates']) or 'žádní'}")
        tp = (self.cfg.get("dip") or {}).get("take_profit_pct", 0)
        for p in self.state.positions("dip"):
            lines.append(f"- [pokles] {p['symbol']}: {p['quantity']} ks, nákup {p['entry_price']:.2f}, "
                         f"prodej při {p['entry_price'] * (1 + tp):.2f}")
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
