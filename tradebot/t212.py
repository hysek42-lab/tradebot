"""Trading 212 Public API client (v0).

Safety rules baked in:
- POST (order) requests are never retried after a network error or timeout,
  because the API is not idempotent and a retry could double the order.
  Instead OrderUncertain is raised and the engine reconciles on the next cycle.
- 429 responses mean the request was NOT executed, so they are retried after
  the reset time.
"""
import base64
import logging
import time

import requests

log = logging.getLogger(__name__)

BASE = {
    "demo": "https://demo.trading212.com/api/v0",
    "live": "https://live.trading212.com/api/v0",
}

# minimum seconds between calls for documented GET limits
MIN_INTERVAL = {
    "/equity/account/summary": 5.2,
    "/equity/metadata/instruments": 51,
    "/equity/metadata/exchanges": 31,
    "/equity/positions": 1.1,
    "/equity/orders": 1.1,
}


class T212Error(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text}")
        self.status = status
        self.text = text


class OrderUncertain(Exception):
    """Order request may or may not have reached the broker."""


class T212Client:
    def __init__(self, env: str, api_key: str, api_secret: str, timeout: float = 15):
        if not api_key or not api_secret:
            raise ValueError("Missing Trading 212 API key/secret (see .env)")
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self.base = BASE[env]
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Basic {token}",
                               "Content-Type": "application/json"})
        self._last_call: dict[str, float] = {}

    def _throttle(self, key: str) -> None:
        gap = MIN_INTERVAL.get(key)
        if not gap:
            return
        wait = self._last_call.get(key, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[key] = time.monotonic()

    def _request(self, method: str, path: str, *, json=None, params=None, throttle_key=None):
        is_order = method in ("POST", "DELETE")
        for attempt in range(4):
            self._throttle(throttle_key or path)
            try:
                r = self.s.request(method, self.base + path, json=json, params=params,
                                   timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if is_order:
                    raise OrderUncertain(f"{method} {path} {json}: {e}") from e
                log.warning("network error on %s %s (%s), retrying", method, path, e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset")
                try:
                    wait = max(1.0, float(reset) - time.time()) if reset else 5.0
                except ValueError:
                    wait = 5.0
                log.info("rate limited on %s, waiting %.0fs", path, wait)
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 408 and is_order:
                raise OrderUncertain(f"{method} {path}: broker timed out")
            if r.status_code >= 400:
                raise T212Error(r.status_code, r.text[:300])
            return r.json() if r.content else None
        raise T212Error(429, f"gave up on {method} {path} after retries")

    # --- account & data -------------------------------------------------
    def summary(self) -> dict:
        return self._request("GET", "/equity/account/summary")

    def positions(self) -> list[dict]:
        return self._request("GET", "/equity/positions") or []

    def instruments(self) -> list[dict]:
        return self._request("GET", "/equity/metadata/instruments") or []

    def active_orders(self) -> list[dict]:
        return self._request("GET", "/equity/orders") or []

    # --- orders ---------------------------------------------------------
    def market_order(self, ticker: str, quantity: float) -> dict:
        return self._request("POST", "/equity/orders/market",
                             json={"ticker": ticker, "quantity": quantity,
                                   "extendedHours": False})

    def stop_order(self, ticker: str, quantity: float, stop_price: float,
                   validity: str = "GOOD_TILL_CANCEL") -> dict:
        return self._request("POST", "/equity/orders/stop",
                             json={"ticker": ticker, "quantity": quantity,
                                   "stopPrice": stop_price, "timeValidity": validity})

    def cancel_order(self, order_id: int) -> None:
        try:
            self._request("DELETE", f"/equity/orders/{order_id}",
                          throttle_key="/equity/orders")
        except T212Error as e:
            if e.status != 404:  # already filled/cancelled
                raise
