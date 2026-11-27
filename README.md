# Obchodní bot pro Trading 212

Bot se sám spouští přes GitHub Actions (nebo běží na serveru), obchoduje podle pevných pravidel přes Trading 212 API
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

## Trhy

Bot obchoduje na dvou trzích, každý ve svých obchodních hodinách (náš čas):
- **USA** 15:30–22:00: technologické a volatilní akcie.
- **Evropa** 9:00–17:30: Xetra, Paříž, Amsterdam.

Evropu vypneš v konfiguraci: `markets → eu → enabled: false`. Limit pozic
(`max_positions`) platí pro oba trhy dohromady. Při prvním běhu dne bot na
Telegram pošle, které tituly ze seznamu v Trading 212 nenašel.

## Agresivní varianta (`config-aggressive.yaml`)

Vědomě hazardní režim: 78 % účtu v aktivních obchodech, 20 % v ETF (S&P 500
a Nasdaq 100) nakupovaných v propadu a držených, 5 % účtu
v riziku na obchod, max. 3 pozice (každá až polovina účtu), volatilnější akcie
(TSLA, COIN, MSTR, IONQ, RKLB…), kratší signály a těsnější stopy.

Navíc má pravidlo **prudkého růstu**: akcie, která dnes vyroste o 7 % a víc,
se kupuje i bez rostoucího trendu a bez proražení maxima (typicky Nvidia +20 %
po výsledcích). Stop-loss je pak širší: max. ~40 % dnešního pohybu zpět.

Zapnutí: v `.env` nastav `BOT_CONFIG=config-aggressive.yaml` a `systemctl restart tradebot`.

Počítej s tím, že série ztrát může účet srazit o třetinu za pár týdnů. Poslední brzda
zůstává: při poklesu o 50 % z maxima se bot zastaví do tvého `/start`.

## Ovládání z mobilu (Telegram)

| Příkaz | Co udělá |
|---|---|
| `/status` | hodnota účtu, pozice, stav bota |
| `/stop` | žádné nové obchody (pozice drží dál se stop-lossy) |
| `/flatten` | prodá všechny aktivní pozice a zastaví se |
| `/start` | znovu spustí obchodování |

Navíc dostaneš zprávu o každém obchodu a denní report ve 22:20.

## Spuštění přes GitHub Actions (zdarma, bez serveru)

Bot se spouští sám každých 5 minut v pracovní dny během obchodních hodin v USA.
GitHub plánované běhy občas o pár minut zpozdí; ochranu před velkou ztrátou drží
stop příkazy přímo u Trading 212, ty na botovi nezávisí.

### 1. Telegram bot
1. V Telegramu napiš **@BotFather** (s modrou fajfkou) → `/newbot` → dostaneš **token**.
2. Napiš svému botovi libovolnou zprávu.
3. Otevři `https://api.telegram.org/bot<TOKEN>/getUpdates` a najdi `"chat":{"id": ...}` → **chat ID**.

### 2. Trading 212 API klíč
Practice (demo) účet → Nastavení → API → vygeneruj klíč s oprávněním pro účet,
portfolio, metadata, historii a zadávání příkazů.

### 3. Klíče do GitHubu (jde i z mobilu v prohlížeči)
Repozitář → **Settings → Secrets and variables → Actions**:

- záložka **Secrets** → **New repository secret**, postupně přidej:
  `T212_DEMO_API_KEY`, `T212_DEMO_API_SECRET`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- záložka **Variables** → **New repository variable**:
  `BOT_CONFIG` = `config-aggressive.yaml` (nebo `config.yaml` pro základní verzi)

### 4. Zapnutí a první test
Záložka **Actions** → pokud se ptá, povol workflows → vyber **tradebot** →
**Run workflow**. Za minutu uvidíš výsledek (zelená = OK) a pak už běží samo.

### 5. Přechod na ostré peníze (po 2–3 týdnech na demu)
Přidej secrets `T212_LIVE_API_KEY` a `T212_LIVE_API_SECRET` a v konfiguraci
přepni `environment: live` (to ti udělám já).

### Limity
- Veřejný repozitář: minuty v Actions zdarma bez omezení.
- Soukromý repozitář: 2 000 minut měsíčně zdarma, bot spotřebuje kolem 2 000–2 400.
  Buď repozitář nech veřejný (kód neobsahuje žádné klíče, ty jsou v Secrets),
  nebo zvětši interval v `.github/workflows/tradebot.yml` na `*/10`.
- Stav bota (pozice, historie) se ukládá do cache GitHub Actions. Kdyby se
  smazala, bot zapomene posuvné stopy, ale stop příkazy u brokera zůstanou.

## Alternativa: vlastní server (VPS)

Kdybys chtěl reakci do minuty, bot umí běžet i nonstop na serveru:

```bash
git clone https://github.com/hysek42-lab/tradebot.git /opt/tradebot
bash /opt/tradebot/deploy/install.sh
nano /opt/tradebot/.env        # vlož klíče
systemctl restart tradebot
```

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
