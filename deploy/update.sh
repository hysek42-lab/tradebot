#!/usr/bin/env bash
# Aktualizace na nejnovější verzi z GitHubu (spusť jako root).
set -euo pipefail
cd /opt/tradebot
git config --global --add safe.directory /opt/tradebot
git pull --ff-only
chown -R tradebot:tradebot /opt/tradebot
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q tests
systemctl restart tradebot
echo ">>> Aktualizováno a restartováno."
