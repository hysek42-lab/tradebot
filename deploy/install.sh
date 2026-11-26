#!/usr/bin/env bash
# Instalace na čerstvý Ubuntu/Debian server. Spusť jako root:
#   git clone <repo> /opt/tradebot && bash /opt/tradebot/deploy/install.sh
set -euo pipefail

APP=/opt/tradebot
apt-get update -y
apt-get install -y python3 python3-venv git

id tradebot &>/dev/null || useradd --system --home "$APP" --shell /usr/sbin/nologin tradebot

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
  echo ">>> Vyplň klíče v $APP/.env (nano $APP/.env) a pak spusť: systemctl restart tradebot"
fi
chmod 600 "$APP/.env"
mkdir -p "$APP/data"
chown -R tradebot:tradebot "$APP"

"$APP/.venv/bin/python" -m pytest -q "$APP/tests"

cp "$APP/deploy/tradebot.service" /etc/systemd/system/tradebot.service
systemctl daemon-reload
systemctl enable tradebot
systemctl restart tradebot
echo ">>> Hotovo. Logy: journalctl -u tradebot -f"
