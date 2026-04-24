#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║              MEMESCANNER — Single File Bot               ║
║  Läuft im Terminal: python3 memescanner.py               ║
║  Benötigt NUR: pip3 install requests                     ║
║  Kein Reddit-Key nötig. Nur Telegram-Token erforderlich. ║
╚══════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────
#  HIER NUR TELEGRAM EINTRAGEN — mehr brauchst du nicht!
# ─────────────────────────────────────────────────────────────

import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────────────────────
#  EINSTELLUNGEN (optional anpassen)
# ─────────────────────────────────────────────────────────────

SCAN_INTERVAL_MINUTES = 10       # Wie oft scannen (Minuten)
MIN_SCORE_TO_ALERT    = 65       # Alert ab diesem Score (0–100)
ALERT_COOLDOWN_HOURS  = 4        # Gleichen Token nicht öfter melden
MIN_LIQUIDITY_USD     = 10_000   # Tokens unter $10k Liquidität ignorieren
MIN_VOLUME_USD        = 5_000    # Tokens unter $5k Volumen ignorieren
MAX_TOKEN_AGE_HOURS   = 72       # Nur Tokens jünger als 3 Tage

CHAINS = ["solana", "ethereum", "bsc", "base"]

SUBREDDITS = [
    "CryptoCurrency", "CryptoMoonShots", "memecoins",
    "SatoshiStreetBets", "solana", "ethtrader"
]

WEIGHTS = {
    "volume_mcap_ratio": 0.25,
    "liquidity_score":   0.15,
    "social_velocity":   0.25,
    "token_age_score":   0.10,
    "holder_growth":     0.10,
    "price_momentum":    0.10,
    "safety_score":      0.05,
}

# ─────────────────────────────────────────────────────────────
#  IMPORTS — nur requests, kein praw nötig
# ─────────────────────────────────────────────────────────────

import sys
import time
import json
import signal
import sqlite3
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("❌ Fehlendes Paket. Bitte ausführen: pip3 install requests")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/scanner.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("scanner")

# ─────────────────────────────────────────────────────────────
#  DATENBANK
# ─────────────────────────────────────────────────────────────

DB_PATH = Path("data/memescanner.db")

def db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            id TEXT PRIMARY KEY, chain TEXT, address TEXT,
            symbol TEXT, name TEXT, first_seen TEXT, last_updated TEXT,
            liquidity_usd REAL, volume_24h REAL, market_cap REAL,
            price_usd REAL, metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT, alerted_at TEXT, score REAL
        );
        CREATE TABLE IF NOT EXISTS social_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, mention_count INTEGER, recorded_at TEXT
        );
        """)
    log.info("Datenbank bereit: %s", DB_PATH)

def upsert_token(t: dict) -> str:
    tid = f"{t['chain']}:{t['address'].lower()}"
    now = datetime.utcnow().isoformat()
    with db() as c:
        exists = c.execute("SELECT id FROM tokens WHERE id=?", (tid,)).fetchone()
        if exists:
            c.execute("""UPDATE tokens SET last_updated=?,liquidity_usd=?,volume_24h=?,
                market_cap=?,price_usd=?,metadata=? WHERE id=?""",
                (now, t.get("liquidity_usd",0), t.get("volume_24h",0),
                 t.get("market_cap",0), t.get("price_usd",0),
                 json.dumps(t.get("metadata",{})), tid))
        else:
            c.execute("""INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, t["chain"], t["address"].lower(), t.get("symbol","?"),
                 t.get("name",""), now, now, t.get("liquidity_usd",0),
                 t.get("volume_24h",0), t.get("market_cap",0),
                 t.get("price_usd",0), json.dumps(t.get("metadata",{}))))
    return tid

def save_social(symbol: str, count: int):
    with db() as c:
        c.execute("INSERT INTO social_mentions VALUES (NULL,?,?,?)",
                  (symbol, count, datetime.utcnow().isoformat()))

def mention_velocity(symbol: str) -> float:
    cutoff_2h  = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with db() as c:
        r = c.execute("SELECT COALESCE(SUM(mention_count),0) FROM social_mentions WHERE symbol=? AND recorded_at>?",
                      (symbol, cutoff_2h)).fetchone()[0]
        d = c.execute("SELECT COALESCE(SUM(mention_count),0) FROM social_mentions WHERE symbol=? AND recorded_at>?",
                      (symbol, cutoff_24h)).fetchone()[0]
    if d == 0: return 1.0
    return (r * 12) / d

def was_alerted_recently(token_id: str) -> bool:
    cutoff = (datetime.utcnow() - timedelta(hours=ALERT_COOLDOWN_HOURS)).isoformat()
    with db() as c:
        row = c.execute("SELECT id FROM alerts WHERE token_id=? AND alerted_at>?",
                        (token_id, cutoff)).fetchone()
    return row is not None

def save_alert(token_id: str, score: float):
    with db() as c:
        c.execute("INSERT INTO alerts VALUES (NULL,?,?,?)",
                  (token_id, datetime.utcnow().isoformat(), score))

# ─────────────────────────────────────────────────────────────
#  DEXSCREENER (kostenlos, kein Key)
# ─────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers["User-Agent"] = "MemeScanner/1.0"

def _get(url: str, params=None) -> dict | None:
    for attempt in range(3):
        try:
            r = _session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.debug("Request fehler (versuch %d): %s", attempt+1, e)
            time.sleep(2 ** attempt)
    return None

def _token_age_hours(pair: dict) -> float:
    ms = pair.get("pairCreatedAt")
    if not ms: return 999
    created = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (datetime.now(tz=timezone.utc) - created).total_seconds() / 3600

def _parse_pair(pair: dict, chain: str) -> dict | None:
    try:
        base      = pair.get("baseToken", {})
        address   = base.get("address", "")
        symbol    = base.get("symbol", "?")
        liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
        volume    = (pair.get("volume") or {}).get("h24", 0) or 0
        price     = float(pair.get("priceUsd") or 0)
        mcap      = pair.get("marketCap") or 0

        if not address or liquidity < MIN_LIQUIDITY_USD or volume < MIN_VOLUME_USD:
            return None
        if _token_age_hours(pair) > MAX_TOKEN_AGE_HOURS:
            return None

        pc  = pair.get("priceChange") or {}
        txn = (pair.get("txns") or {}).get("h24") or {}
        buys  = txn.get("buys",  0) or 0
        sells = txn.get("sells", 0) or 0

        return {
            "chain":        chain,
            "address":      address,
            "symbol":       symbol,
            "name":         base.get("name", ""),
            "liquidity_usd": liquidity,
            "volume_24h":   volume,
            "market_cap":   float(mcap),
            "price_usd":    price,
            "age_hours":    _token_age_hours(pair),
            "dex_url":      pair.get("url", ""),
            "metadata": {
                "price_change_h1":  (pc.get("h1")  or 0),
                "price_change_h6":  (pc.get("h6")  or 0),
                "price_change_h24": (pc.get("h24") or 0),
                "txns_buys_24h":    buys,
                "txns_sells_24h":   sells,
                "buy_sell_ratio":   buys / max(sells, 1),
            }
        }
    except Exception as e:
        log.debug("Parse error: %s", e)
        return None

def scan_dex() -> list[dict]:
    all_tokens = {}
    meme_terms = ["meme","pepe","doge","cat","inu","moon","chad","wagmi","gm","wojak","frog","shib","bonk"]

    for term in meme_terms:
        data = _get("https://api.dexscreener.com/latest/dex/search", params={"q": term})
        for pair in (data or {}).get("pairs") or []:
            chain = pair.get("chainId", "")
            if chain not in CHAINS: continue
            t = _parse_pair(pair, chain)
            if t:
                tid = f"{t['chain']}:{t['address'].lower()}"
                if tid not in all_tokens or all_tokens[tid]["liquidity_usd"] < t["liquidity_usd"]:
                    all_tokens[tid] = t
        time.sleep(0.4)

    # Boosted tokens
    data = _get("https://api.dexscreener.com/token-boosts/latest/v1")
    if data and isinstance(data, list):
        for item in data[:20]:
            chain   = item.get("chainId", "")
            address = item.get("tokenAddress", "")
            if chain not in CHAINS or not address: continue
            pd = _get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
            for pair in (pd or {}).get("pairs") or []:
                t = _parse_pair(pair, chain)
                if t:
                    tid = f"{t['chain']}:{t['address'].lower()}"
                    if tid not in all_tokens:
                        all_tokens[tid] = t
                    break

    log.info("DEXScreener: %d qualifizierte Token gefunden", len(all_tokens))
    return list(all_tokens.values())

# ─────────────────────────────────────────────────────────────
#  REDDIT — öffentliche JSON-API, KEIN KEY NÖTIG
# ─────────────────────────────────────────────────────────────

_reddit_cache      = []
_reddit_cache_time = None
_reddit_headers    = {
    "User-Agent": "Mozilla/5.0 MemeScanner/1.0"
}

def get_reddit_posts() -> list[dict]:
    global _reddit_cache, _reddit_cache_time
    now = datetime.utcnow()
    if _reddit_cache_time and (now - _reddit_cache_time).seconds < 480:
        return _reddit_cache

    posts = []
    for sub in SUBREDDITS:
        try:
            url = f"https://www.reddit.com/r/{sub}/new.json"
            r   = requests.get(url, headers=_reddit_headers, params={"limit": 25}, timeout=10)
            if r.status_code != 200:
                log.warning("Reddit r/%s: HTTP %d", sub, r.status_code)
                continue
            for child in r.json().get("data", {}).get("children", []):
                d = child.get("data", {})
                posts.append({
                    "title": d.get("title", ""),
                    "body":  d.get("selftext", ""),
                    "score": d.get("score", 0),
                    "comments": d.get("num_comments", 0),
                })
            time.sleep(1.5)  # Reddit rate limit respektieren
        except Exception as e:
            log.warning("Reddit r/%s Fehler: %s", sub, e)

    _reddit_cache      = posts
    _reddit_cache_time = now
    log.info("Reddit: %d Posts geladen (kein Key nötig)", len(posts))
    return posts

def reddit_mentions(symbol: str, posts: list) -> int:
    pat = re.compile(
        rf'(?<!\w){re.escape(symbol.upper())}(?!\w)|\${re.escape(symbol.upper())}',
        re.IGNORECASE
    )
    return sum(1 for p in posts if pat.search(f"{p['title']} {p['body']}"))

# ─────────────────────────────────────────────────────────────
#  SCORING ENGINE (0–100)
# ─────────────────────────────────────────────────────────────

def score_vol_mcap(t: dict) -> float:
    v = t.get("volume_24h", 0)
    m = t.get("market_cap", 0)
    if m <= 0:
        return 70 if v > 500_000 else 50 if v > 50_000 else 30
    r = v / m
    if r >= 2.0: return 100
    if r >= 1.0: return 90
    if r >= 0.5: return 75
    if r >= 0.2: return 55
    if r >= 0.1: return 35
    return max(0, r / 0.1 * 35)

def score_liquidity(t: dict) -> float:
    liq = t.get("liquidity_usd", 0)
    if liq >= 500_000: return 100
    if liq >= 100_000: return 80
    if liq >= 50_000:  return 60
    if liq >= 10_000:  return 40
    return max(0, (liq / 10_000) * 40)

def score_social(symbol: str, posts: list) -> float:
    count    = reddit_mentions(symbol, posts)
    velocity = mention_velocity(symbol.upper())
    if count > 0:
        save_social(symbol.upper(), count)
    base  = 90 if count >= 30 else 65 if count >= 10 else 40 if count >= 2 else 20 if count >= 1 else 0
    boost = 20 if velocity >= 3 else 12 if velocity >= 2 else 6 if velocity >= 1.5 else 0
    return min(100, base + boost)

def score_age(t: dict) -> float:
    a = t.get("age_hours", 999)
    if a <= 3:  return 100
    if a <= 6:  return 90
    if a <= 12: return 75
    if a <= 24: return 60
    if a <= 48: return 35
    if a <= 72: return 15
    return 0

def score_momentum(t: dict) -> float:
    m   = t.get("metadata", {})
    h1  = m.get("price_change_h1",  0) or 0
    h6  = m.get("price_change_h6",  0) or 0
    h24 = m.get("price_change_h24", 0) or 0
    bsr = m.get("buy_sell_ratio",   1) or 1
    wc  = h1 * 0.5 + h6 * 0.3 + h24 * 0.2
    ms  = 100 if wc>=100 else 85 if wc>=50 else 70 if wc>=20 else \
          55 if wc>=10 else 40 if wc>=0 else max(0, 40 + wc * 0.5)
    boost = 15 if bsr>=2.5 else 8 if bsr>=1.5 else 0 if bsr>=1 else -10
    return min(100, max(0, ms + boost))

def score_safety(t: dict) -> float:
    liq = t.get("liquidity_usd", 0)
    m   = t.get("market_cap", 0)
    bsr = t.get("metadata", {}).get("buy_sell_ratio", 1) or 1
    s   = 70
    if m > 0:
        r = liq / m
        s += 20 if r>=0.3 else 10 if r>=0.1 else -20 if r<0.02 else 0
    if bsr > 5:    s -= 15
    elif bsr < 0.3: s -= 20
    return min(100, max(0, s))

def compute_score(t: dict, posts: list) -> dict:
    sym = t.get("symbol", "?").upper()
    bd = {
        "volume_mcap_ratio": score_vol_mcap(t),
        "liquidity_score":   score_liquidity(t),
        "social_velocity":   score_social(sym, posts),
        "token_age_score":   score_age(t),
        "holder_growth":     40,
        "price_momentum":    score_momentum(t),
        "safety_score":      score_safety(t),
    }
    total = round(sum(bd[k] * WEIGHTS[k] for k in bd), 1)

    price = t.get("price_usd", 0)
    mcap  = t.get("market_cap", 0)
    age   = t.get("age_hours", 48)
    am    = max(0.5, 2.0 - (age / 72))

    if total >= 90:   lo, hi, conf = 10*am, 100*am, "HIGH"
    elif total >= 80: lo, hi, conf = 5*am,  30*am,  "MEDIUM-HIGH"
    elif total >= 70: lo, hi, conf = 2*am,  10*am,  "MEDIUM"
    elif total >= 60: lo, hi, conf = 1.5,   5*am,   "LOW-MEDIUM"
    else:             lo, hi, conf = 1.0,   2.0,    "LOW"

    return {
        "total": total,
        "grade": "🔥 S-TIER" if total>=85 else "🚀 A-TIER" if total>=75 else "⚡ B-TIER" if total>=65 else "📈 C-TIER",
        "bd":    bd,
        "target": {
            "now":       price,
            "low":       round(price * lo, 12) if price else 0,
            "high":      round(price * hi, 12) if price else 0,
            "mult_low":  round(lo, 1),
            "mult_high": round(hi, 1),
            "mcap_high_m": round(mcap * hi / 1e6, 2) if mcap else None,
            "conf":      conf,
        }
    }

# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────

def tg(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
        return r.status_code == 200
    except Exception as e:
        log.error("Telegram Fehler: %s", e)
        return False

def fmt_price(p: float) -> str:
    if p == 0:       return "N/A"
    if p < 0.000001: return f"${p:.2e}"
    if p < 0.01:     return f"${p:.8f}"
    if p < 1:        return f"${p:.6f}"
    return f"${p:.4f}"

def fmt_usd(n: float) -> str:
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 1_000:     return f"${n/1_000:.1f}K"
    return f"${n:.0f}"

def bar(score: float, w=10) -> str:
    f = int((score / 100) * w)
    return "█" * f + "░" * (w - f)

def mbar(score: float, w=5) -> str:
    f = int((score / 100) * w)
    return "▓" * f + "░" * (w - f)

def send_alert(t: dict, s: dict):
    sym   = t.get("symbol", "?")
    name  = t.get("name", sym)
    chain = t.get("chain", "?").upper()
    age   = t.get("age_hours", 0)
    age_s = f"{int(age*60)}min" if age < 1 else f"{age:.1f}h"
    meta  = t.get("metadata", {})
    tg_   = s["target"]
    bd    = s["bd"]
    bsr   = meta.get("buy_sell_ratio", 1) or 1
    h1    = meta.get("price_change_h1",  0) or 0
    h24   = meta.get("price_change_h24", 0) or 0

    msg = f"""🔍 <b>MEME COIN ALERT</b> — {s['grade']}

<b>${sym}</b> ({name}) · {chain}
⏱ {age_s} old  |  Score: <b>{s['total']}/100</b>
{bar(s['total'])} {s['total']}%

━━ 💰 MARKET DATA ━━
Price:      {fmt_price(t.get('price_usd',0))}
Market Cap: {fmt_usd(t.get('market_cap',0)) if t.get('market_cap') else 'N/A'}
Liquidity:  {fmt_usd(t.get('liquidity_usd',0))}
Vol 24h:    {fmt_usd(t.get('volume_24h',0))}
Δ 1h:  {h1:+.1f}%  |  Δ 24h: {h24:+.1f}%
Buy/Sell:   {bsr:.2f}x

━━ 🎯 PRICE TARGET ━━
Confidence: <b>{tg_['conf']}</b>
Now:    {fmt_price(tg_['now'])}
Low ↑:  {fmt_price(tg_['low'])}  ({tg_['mult_low']}x)
High ↑↑:{fmt_price(tg_['high'])} ({tg_['mult_high']}x)
{f"MCap @ target: ${tg_['mcap_high_m']}M" if tg_.get('mcap_high_m') else ''}

━━ 📊 SCORE BREAKDOWN ━━
Vol/MCap:  {mbar(bd['volume_mcap_ratio'])} {bd['volume_mcap_ratio']:.0f}
Liquidity: {mbar(bd['liquidity_score'])} {bd['liquidity_score']:.0f}
Social:    {mbar(bd['social_velocity'])} {bd['social_velocity']:.0f}
Age:       {mbar(bd['token_age_score'])} {bd['token_age_score']:.0f}
Momentum:  {mbar(bd['price_momentum'])} {bd['price_momentum']:.0f}
Safety:    {mbar(bd['safety_score'])} {bd['safety_score']:.0f}

{t.get('dex_url','') or 'No DEX link available'}

⚠️ <i>Not financial advice. Meme coins are extremely high risk.</i>"""

    tg(msg)

# ─────────────────────────────────────────────────────────────
#  HAUPTSCHLEIFE
# ─────────────────────────────────────────────────────────────

running = True

def on_signal(sig, frame):
    global running
    print("\n⛔  Stopp-Signal — beende sauber...")
    running = False

signal.signal(signal.SIGINT,  on_signal)
signal.signal(signal.SIGTERM, on_signal)

def check_config():
    errors = []
    if "DEIN_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN nicht gesetzt (Schritt 1 in der Anleitung)")
    if "DEINE_CHAT_ID" in TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID nicht gesetzt (Schritt 2 in der Anleitung)")
    return errors

def run_cycle(stats: dict):
    log.info("═══ Scan-Zyklus #%d ═══", stats["cycles"] + 1)

    tokens = scan_dex()
    if not tokens:
        log.warning("Keine Token vom DEX zurückgekommen")
        return

    posts   = get_reddit_posts()
    alerted = []

    for t in tokens:
        try:
            result   = compute_score(t, posts)
            token_id = upsert_token(t)
            stats["scanned"] += 1

            log.info("  %-10s  Score:%5.1f  Vol:%-8s  Liq:%-8s  Alter:%.1fh",
                     t.get("symbol","?"), result["total"],
                     fmt_usd(t.get("volume_24h",0)),
                     fmt_usd(t.get("liquidity_usd",0)),
                     t.get("age_hours",0))

            if result["total"] >= MIN_SCORE_TO_ALERT:
                if not was_alerted_recently(token_id):
                    log.info("  🔔 ALERT: %s — Score %.1f", t.get("symbol","?"), result["total"])
                    send_alert(t, result)
                    save_alert(token_id, result["total"])
                    stats["alerts"] += 1
                    alerted.append((t.get("symbol","?"), result["total"]))
                else:
                    log.info("  ⏭  %s bereits gemeldet — überspringe", t.get("symbol","?"))

        except Exception as e:
            log.warning("Fehler bei %s: %s", t.get("symbol"), e)

    stats["cycles"] += 1
    if alerted:
        log.info("Zyklus: %d Alerts → %s",
                 len(alerted), ", ".join(f"${s}({sc:.0f})" for s,sc in alerted))
    else:
        log.info("Zyklus abgeschlossen — kein Alert (alle Scores unter %d)", MIN_SCORE_TO_ALERT)

def main():
    print("""
╔══════════════════════════════════════════════════╗
║          MEMESCANNER  —  wird gestartet...       ║
║  Abbrechen: Ctrl+C                               ║
║  Kein Reddit-Key nötig — läuft sofort!           ║
╚══════════════════════════════════════════════════╝
""")

    errors = check_config()
    if errors:
        print("❌ Konfigurationsfehler:\n" + "\n".join(f"   • {e}" for e in errors))
        print("""
─────────────────────────────────────────
 WIE DU DEN TELEGRAM TOKEN BEKOMMST:
─────────────────────────────────────────
 1. Öffne Telegram
 2. Suche nach: @BotFather
 3. Schreibe: /newbot
 4. Folge den Anweisungen → du bekommst einen Token
 5. Trage ihn oben im Script ein bei TELEGRAM_BOT_TOKEN

 WIE DU DEINE CHAT ID BEKOMMST:
 1. Schreibe deinem neuen Bot irgend etwas
 2. Öffne im Browser:
    https://api.telegram.org/bot<DEIN_TOKEN>/getUpdates
 3. Kopiere die Zahl bei "id": unter "chat"
 4. Trage sie ein bei TELEGRAM_CHAT_ID
─────────────────────────────────────────
""")
        sys.exit(1)

    init_db()

    tg("🤖 <b>MemeScanner started</b>\n"
       "Scanning DEX + Reddit every 10 minutes.\n"
       f"Alert threshold: Score ≥ {MIN_SCORE_TO_ALERT}/100\n"
       f"Reddit: no API key — public JSON feed\n"
       f"{datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC")

    log.info("Bot läuft. Erster Scan beginnt sofort.")

    stats    = {"scanned": 0, "alerts": 0, "cycles": 0}
    interval = SCAN_INTERVAL_MINUTES * 60

    while running:
        try:
            run_cycle(stats)
        except Exception as e:
            log.exception("Unerwarteter Fehler: %s", e)
            tg(f"⚠️ <b>Error</b>\n<code>{e}</code>")

        log.info("Warte %d Minuten...\n", SCAN_INTERVAL_MINUTES)
        for _ in range(interval):
            if not running: break
            time.sleep(1)

    log.info("MemeScanner gestoppt.")

if __name__ == "__main__":
    main()
