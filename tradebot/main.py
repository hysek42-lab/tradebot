"""Entry point: `python -m tradebot.main` (runs forever) or `--once` (single cycle)."""
import argparse
import logging
import time
import traceback
from logging.handlers import RotatingFileHandler

from .config import load_config
from .data import MarketData
from .engine import Engine
from .notify import Telegram
from .state import State
from .t212 import T212Client


def build(cfg) -> Engine:
    client = T212Client(cfg["environment"], cfg["api_key"], cfg["api_secret"])
    tg = Telegram(cfg["telegram_token"], cfg["telegram_chat_id"])
    state = State(cfg["data_dir"] / "bot.sqlite")
    return Engine(cfg, client, MarketData(), state, tg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    cfg = load_config()
    handlers = [logging.StreamHandler(),
                RotatingFileHandler(cfg["data_dir"] / "bot.log", maxBytes=2_000_000, backupCount=5)]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("tradebot")

    engine = build(cfg)
    if args.once:
        engine.cycle()
        return

    engine.tg.send(f"Bot spuštěn ({cfg['environment']}{', dry run' if cfg.get('dry_run') else ''}).")
    next_cycle, last_error_msg = 0.0, 0.0
    while True:
        try:
            now = time.time()
            if now >= next_cycle:
                engine.cycle()
                open_ = engine.market_open()
                next_cycle = now + (300 if open_ else 900)
            else:
                engine.handle_commands()
        except Exception as e:  # keep running; report at most once per 30 min
            log.error("cycle failed: %s\n%s", e, traceback.format_exc())
            if time.time() - last_error_msg > 1800:
                engine.tg.send(f"Chyba v cyklu: {e}")
                last_error_msg = time.time()
            next_cycle = time.time() + 300
        time.sleep(30)


if __name__ == "__main__":
    main()
