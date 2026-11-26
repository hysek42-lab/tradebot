"""Telegram notifications and phone commands (/status, /stop, /start, /flatten)."""
import logging

import requests

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.enabled = bool(token and chat_id)
        self._offset = 0

    def send(self, text: str) -> None:
        log.info("NOTIFY: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except requests.RequestException as e:
            log.warning("telegram send failed: %s", e)

    def commands(self) -> list[str]:
        """New commands from the authorised chat only."""
        if not self.enabled:
            return []
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                             params={"offset": self._offset, "timeout": 0}, timeout=10)
            updates = r.json().get("result", [])
        except (requests.RequestException, ValueError) as e:
            log.warning("telegram poll failed: %s", e)
            return []
        cmds = []
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue  # ignore strangers
            text = (msg.get("text") or "").strip().lower()
            if text.startswith("/"):
                cmds.append(text.split()[0].split("@")[0])
        return cmds
