"""
Monitor Polymarket Fútbol v2.1 → Telegram
Vigila el feed global de trades: alerta ballenas en fútbol (>= WHALE_USD)
y trades de top traders del leaderboard (>= MIN_USD).
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WALLETS_EXTRA = [w.strip().lower() for w in os.environ.get("WALLETS_EXTRA", "").split(",") if w.strip()]
TOP_N = int(os.environ.get("TOP_N", "50"))
LEADERBOARD_WINDOW = os.environ.get("LEADERBOARD_WINDOW", "30d")
MIN_USD = float(os.environ.get("MIN_USD", "500"))
WHALE_USD = float(os.environ.get("WHALE_USD", "2000"))
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "6"))

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; futbol-monitor/2.1)"}

SOCCER_KEYWORDS = [
    "soccer", "premier league", "epl", "la liga", "laliga", "serie a",
    "bundesliga", "ligue 1", "champions league", "ucl", "europa league",
    "world cup", "copa america", "copa libertadores", "copa del rey",
    "fifa", "uefa", "conmebol", "mls", "liga mx", "eredivisie",
    "fa cup", "carabao", "supercopa", "ballon d'or", "golden boot",
    "relegat", "top scorer", "win the league",
]


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize_ts(raw):
    """Normaliza timestamps que pueden venir en segundos, milisegundos o texto."""
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return 0
    if ts > 1e12:  # milisegundos
        ts = ts / 1000
    return int(ts)


def safe_get(url, params=None, timeout=25):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log(f"HTTP {r.status_code} en {url}")
    except Exception as e:
        log(f"Error en {url}: {e}")
    return None


def get_top_traders():
    traders = {}
    data = safe_get(f"{LB_API}/profit", params={"window": LEADERBOARD_WINDOW, "limit": TOP_N})
    if not data:
        data = safe_get(f"{LB_API}/leaderboard", params={"window": LEADERBOARD_WINDOW, "limit": TOP_N, "rankType": "profit"})
    if isinstance(data, list):
        for t in data:
            wallet = (t.get("proxyWallet") or t.get("wallet") or t.get("address") or "").lower()
            name = t.get("name") or t.get("pseudonym") or wallet[:8]
            if wallet:
                traders[wallet] = name
    log(f"Leaderboard: {len(traders)} traders obtenidos")
    return traders


def get_soccer_market_slugs():
    slugs, condition_ids = set(), set()
    for tag in ["soccer", "football"]:
        data = safe_get(f"{GAMMA_API}/events", params={
            "tag_slug": tag, "active": "true", "closed": "false", "limit": 200
        })
        if isinstance(data, list):
            for ev in data:
                if ev.get("slug"):
                    slugs.add(ev["slug"].lower())
                for m in ev.get("markets", []) or []:
                    if m.get("conditionId"):
                        condition_ids.add(m["conditionId"].lower())
    log(f"Fútbol activo: {len(slugs)} eventos, {len(condition_ids)} mercados")
    return slugs, condition_ids


def is_soccer_trade(trade, soccer_slugs, soccer_condition_ids):
    cond = (trade.get("conditionId") or "").lower()
    if cond and cond in soccer_condition_ids:
        return True
    event_slug = (trade.get("eventSlug") or "").lower()
    if event_slug and event_slug in soccer_slugs:
        return True
    text = f"{trade.get('title','')} {trade.get('eventSlug','')} {trade.get('slug','')}".lower()
    return any(kw in text for kw in SOCCER_KEYWORDS)


def get_global_trades(cutoff_ts):
    """Feed global de trades recientes de todo Polymarket."""
    all_trades, offset = [], 0
    while offset <= 2000:
        batch = safe_get(f"{DATA_API}/trades", params={"limit": 500, "offset": offset, "takerOnly": "true"})
        if not isinstance(batch, list) or not batch:
            break
        all_trades.extend(batch)
        oldest = min(normalize_ts(t.get("timestamp")) for t in batch)
        if oldest < cutoff_ts:
            break
        offset += 500
    if all_trades:
        newest = max(normalize_ts(t.get("timestamp")) for t in all_trades)
        log(f"Feed crudo: {len(all_trades)} trades, más reciente hace {int(time.time()) - newest}s")
    else:
        log("Feed crudo: vacío — el endpoint no devolvió datos")
    recent = [t for t in all_trades if normalize_ts(t.get("timestamp")) >= cutoff_ts]
    log(f"Feed global: {len(recent)} trades en la ventana")
    return recent


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Telegram excepción: {e}")


def format_alert(trade, tag, trader_name, wallet):
    side = "🟢 COMPRA" if str(trade.get("side", "")).upper() == "BUY" else "🔴 VENTA"
    size = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    usd = size * price
    outcome = trade.get("outcome", "?")
    title = trade.get("title", "Mercado desconocido")
    event_slug = trade.get("eventSlug") or trade.get("slug") or ""
    link = f"https://polymarket.com/event/{event_slug}" if event_slug else "https://polymarket.com"
    profile = f"https://polymarket.com/profile/{wallet}" if wallet else ""
    ts = datetime.fromtimestamp(normalize_ts(trade.get("timestamp")), tz=timezone.utc).strftime("%H:%M UTC")
    return (
        f"{tag}\n\n"
        f"👤 <a href='{profile}'>{trader_name}</a>\n"
        f"{side} <b>{outcome}</b>\n"
        f"📊 {title}\n\n"
        f"💵 Monto: <b>${usd:,.0f}</b>\n"
        f"🎯 Precio: {price:.2f} (prob. implícita {price*100:.0f}%)\n"
        f"🕐 {ts}\n"
        f"🔗 <a href='{link}'>Ver mercado</a>"
    )


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        sys.exit(1)

    cutoff_ts = int(time.time()) - WINDOW_MINUTES * 60

    traders = get_top_traders()
    for w in WALLETS_EXTRA:
        traders.setdefault(w, f"Seguido {w[:8]}")

    soccer_slugs, soccer_condition_ids = get_soccer_market_slugs()
    trades = get_global_trades(cutoff_ts)

    alerts, seen = 0, set()
    for trade in trades:
        tx = trade.get("transactionHash") or f"{trade.get('timestamp')}-{trade.get('asset')}-{trade.get('proxyWallet')}"
        if tx in seen:
            continue
        seen.add(tx)

        if not is_soccer_trade(trade, soccer_slugs, soccer_condition_ids):
            continue

        usd = float(trade.get("size", 0)) * float(trade.get("price", 0))
        wallet = (trade.get("proxyWallet") or "").lower()
        trader_name = trade.get("pseudonym") or trade.get("name") or (traders.get(wallet) or wallet[:8])

        if wallet in traders and usd >= MIN_USD:
            tag = "⭐ <b>TOP TRADER APUESTA FÚTBOL</b>"
        elif usd >= WHALE_USD:
            tag = "🐋 <b>BALLENA EN FÚTBOL</b>"
        else:
            continue

        send_telegram(format_alert(trade, tag, trader_name, wallet))
        alerts += 1
        time.sleep(1)

    log(f"Listo. {alerts} alertas enviadas.")


if __name__ == "__main__":
    main()
