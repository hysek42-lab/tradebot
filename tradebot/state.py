"""Persistent bot state in SQLite: managed positions, trades, equity history, flags."""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  ticker TEXT PRIMARY KEY,      -- Trading 212 ticker
  symbol TEXT NOT NULL,         -- Yahoo symbol
  bucket TEXT NOT NULL,         -- 'active' or 'core'
  quantity REAL NOT NULL,
  entry_price REAL NOT NULL,    -- instrument currency
  stop REAL,                    -- current software stop (instrument currency)
  highest REAL,                 -- highest price since entry
  atr REAL,
  broker_stop_id INTEGER,       -- protective stop order at the broker
  opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, ticker TEXT, side TEXT, quantity REAL, price REAL,
  bucket TEXT, reason TEXT, pnl REAL
);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL NOT NULL, day TEXT NOT NULL, total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
  ticker TEXT PRIMARY KEY, ts REAL NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # key-value flags ----------------------------------------------------
    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    # positions ----------------------------------------------------------
    def positions(self, bucket: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM positions", ()
        if bucket:
            q, args = q + " WHERE bucket=?", (bucket,)
        return [dict(r) for r in self.db.execute(q, args)]

    def position(self, ticker: str) -> dict | None:
        r = self.db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        return dict(r) if r else None

    def upsert_position(self, p: dict) -> None:
        cols = ["ticker", "symbol", "bucket", "quantity", "entry_price", "stop",
                "highest", "atr", "broker_stop_id", "opened_at"]
        self.db.execute(
            f"INSERT OR REPLACE INTO positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [p.get(c) for c in cols])
        self.db.commit()

    def remove_position(self, ticker: str) -> None:
        self.db.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
        self.db.commit()

    # trades & equity ----------------------------------------------------
    def log_trade(self, ticker, side, qty, price, bucket, reason, pnl=None) -> None:
        self.db.execute("INSERT INTO trades (ts,ticker,side,quantity,price,bucket,reason,pnl) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (time.time(), ticker, side, qty, price, bucket, reason, pnl))
        self.db.commit()

    def trades_since(self, ts: float) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM trades WHERE ts>=? ORDER BY ts", (ts,))]

    def log_equity(self, day: str, total: float) -> None:
        self.db.execute("INSERT INTO equity VALUES (?,?,?)", (time.time(), day, total))
        self.db.commit()

    def day_start_equity(self, day: str) -> float | None:
        r = self.db.execute("SELECT total FROM equity WHERE day=? ORDER BY ts LIMIT 1",
                            (day,)).fetchone()
        return r["total"] if r else None

    def peak_equity(self, since_ts: float = 0) -> float | None:
        r = self.db.execute("SELECT MAX(total) AS m FROM equity WHERE ts>=?",
                            (since_ts,)).fetchone()
        return r["m"]

    # order intents (duplicate-order guard) ------------------------------
    def add_intent(self, ticker: str, detail: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO intents VALUES (?,?,?)",
                        (ticker, time.time(), detail))
        self.db.commit()

    def has_recent_intent(self, ticker: str, within_s: float = 1800) -> bool:
        r = self.db.execute("SELECT ts FROM intents WHERE ticker=?", (ticker,)).fetchone()
        return bool(r and time.time() - r["ts"] < within_s)

    def get_intent(self, ticker: str) -> str | None:
        r = self.db.execute("SELECT detail FROM intents WHERE ticker=?", (ticker,)).fetchone()
        return r["detail"] if r else None

    def clear_intent(self, ticker: str) -> None:
        self.db.execute("DELETE FROM intents WHERE ticker=?", (ticker,))
        self.db.commit()
