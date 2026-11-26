# Obchodní bot pro Trading 212

Bot běží nonstop na malém serveru, obchoduje podle pevných pravidel přes Trading 212 API
a posílá ti zprávy na Telegram. Žádné AI za běhu, žádné kredity.

> Experiment s penězi, které můžeš ztratit. Strategie vychází z ověřených principů
> (sledování trendu, stop-lossy, řízení velikosti pozic), ale zisk nezaručuje nic.

## Jak obchoduje

**70 % – aktivní část (momentum breakout).** Každých 15 minut během otevřené burzy v USA
(15:30–22:00 našeho času) projde seznam ~30 likvidních a volatilních akcií. Koupí, když
akcie prorazí nad 20denní maximum a je v rostoucím trendu. Vybírá nejsilnější podle
3měsíčního momenta.

- Stop-loss: vstup − 2× ATR (průměrný denní rozkmit), zadaný i jako stop příkaz u brokera,
  takže chrání i když bot spadne.
- Posuvný stop: maximum od nákupu − 3× ATR, posouvá se jen nahoru a zamyká zisk.
- Max. 15 obchodních dní držení, max. 5 pozic najednou.
- Jeden obchod smí při trefení stop-lossu ztratit max. 2,5 % aktivního kapitálu.

**30 % – dlouhodobá část (ETF).** Každé pondělí dokupuje S&P 500 a FTSE All-World ETF
k cílové váze. V propadu (10 % a víc pod ročním maximem) dokoupí celý rozdíl, jinak jen
čtvrtinu. ETF nikdy neprodává.

**Pojistky:**
- Denní ztráta 6 % → ten den žádné nové obchody.
- Pokles 35 % z maxima → bot se zastaví úplně, dokud mu nepošleš `/start`.
- Nikdy neopakuje příkaz, u kterého si není jistý, že prošel (API Trading 212 není
  idempotentní a opakování by mohlo nakoupit dvakrát).

Všechny parametry jsou v `config.yaml`.

## Ovládání z mobilu (Telegram)

| Příkaz | Co udělá |
|---|---|
| `/status` | hodnota účtu, pozice, stav bota |
| `/stop` | žádné nové obchody (pozice drží dál se stop-lossy) |
| `/flatten` | prodá všechny aktivní pozice a zastaví se |
| `/start` | znovu spustí obchodování |

Navíc dostaneš zprávu o každém obchodu a denní report ve 22:20.

## Nastavení krok za krokem

### 1. Telegram bot (5 minut)
1. V Telegramu napiš **@BotFather** → `/newbot` → zvol jméno → dostaneš **token**.
2. Napiš svému novému botovi libovolnou zprávu.
3. Otevři v prohlížeči `https://api.telegram.org/bot<TOKEN>/getUpdates` a najdi
   `"chat":{"id": ...}` → to je **chat ID**.

### 2. Trading 212 API klíč
V aplikaci přepni na **Practice (demo)** účet → Nastavení → API → vygeneruj klíč.
Povol oprávnění pro čtení účtu, portfolia, metadat a zadávání příkazů.
Klíč i secret si ulož, ukáže se jen jednou.

### 3. Server
Nejlevnější VPS s Ubuntu 24.04 (např. Hetzner, DigitalOcean, Contabo – pár eur měsíčně) stačí.
Přihlas se přes SSH (z mobilu třeba aplikací Termius) a spusť:

```bash
git clone https://github.com/<tvuj-ucet>/tradebot.git /opt/tradebot
bash /opt/tradebot/deploy/install.sh
nano /opt/tradebot/.env        # vlož klíče z kroků 1 a 2
systemctl restart tradebot
```

Do 1 minuty ti na Telegram přijde „Bot spuštěn (demo)“.
Doporučení: v Trading 212 omez API klíč na IP adresu serveru.

### 4. Testovací provoz
Nech bota 2–3 týdny běžet na demu. Pak v `config.yaml` přepni `environment: live`,
doplň do `.env` ostré klíče a spusť `bash /opt/tradebot/deploy/update.sh`.

## Užitečné příkazy na serveru

```bash
journalctl -u tradebot -f           # živé logy
systemctl stop tradebot             # vypnout
bash /opt/tradebot/deploy/update.sh # stáhnout novou verzi a restartovat
```

## Struktura

```
tradebot/t212.py        API klient (ochrana proti duplicitním příkazům, rate limity)
tradebot/data.py        tržní data z Yahoo Finance
tradebot/strategies.py  vstupy, výstupy, ETF nákupy (čisté funkce)
tradebot/risk.py        pojistky a velikost pozic
tradebot/engine.py      řízení celého cyklu
tradebot/notify.py      Telegram
tests/                  testy se simulovaným brokerem
```

## Omezení, o kterých je dobré vědět
- Data z Yahoo jsou neoficiální a občas vypadnou; bot pak daný cyklus přeskočí.
- Posuvný stop hlídá bot softwarově; u brokera zůstává jen počáteční stop-loss.
  Když server vypadne, pozici chrání pořád ten počáteční.
- Bot počítá s tím, že na účtu obchoduje jen on. Ruční obchody na stejném účtu nedělej.
- Daně: časté obchodování může překročit limit 100 000 Kč příjmů z prodejů za rok.
  Ověř si u daňaře.
