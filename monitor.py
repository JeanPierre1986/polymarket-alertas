"""
Monitor Polymarket Deportes v4.3 → Telegram
Vigila los TOP 50 traders y alerta apuestas deportivas >= $500.
Con detalle de tipo de O/U (Goles, Corners, Puntos, etc).
"""

import os
import sys
import time
import re
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WALLETS_EXTRA = [w.strip().lower() for w in os.environ.get("WALLETS_EXTRA", "").split(",") if w.strip()]
TOP_N = int(os.environ.get("TOP_N", "80"))
LEADERBOARD_WINDOW = os.environ.get("LEADERBOARD_WINDOW", "30d")
MIN_USD = float(os.environ.get("MIN_USD", "500"))
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "4"))

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sports-monitor/4.3)"}

SPORT_TAGS = ["sports", "soccer", "football", "nba", "nfl", "mlb", "nhl", "tennis", "ufc", "mma", "boxing", "golf", "f1", "cricket", "esports"]

SPORT_KEYWORDS = ["soccer", "premier league", "epl", "la liga", "laliga", "serie a", "bundesliga", "ligue 1", "champions league", "ucl", "europa league", "world cup", "copa america", "copa libertadores", "fifa", "uefa", "mls", "liga mx", "fa cup", "golden boot", "top scorer", "nba", "nfl", "mlb", "nhl", "super bowl", "world series", "stanley cup", "finals", "playoffs", "grand slam", "wimbledon", "us open", "roland garros", "australian open", "ufc", "mma", "boxing", "heavyweight", "grand prix", "formula 1", "f1 ", "premier padel", "olympics", "ncaa", "march madness", "masters", "pga", "ryder cup", "cricket", "ipl", "rugby", "atp", "wta", " vs ", " vs. ", " @ "]


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize_ts(raw):
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return 0
    if ts > 1e12:
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
        for i, t in enumerate(data[:TOP_N]):
            wallet = (t.get("proxyWallet") or t.get("wallet") or t.get("address") or "").lower()
            name = t.get("name") or t.get("pseudonym") or wallet[:8]
            if wallet:
                traders[wallet] = (name, i + 1)
    log(f"Leaderboard: {len(traders)} traders obtenidos")
    return traders


def get_sport_market_ids():
    slugs, condition_ids = set(), set()
    for tag in SPORT_TAGS:
        data = safe_get(f"{GAMMA_API}/events", params={"tag_slug": tag, "active": "true", "closed": "false", "limit": 200})
        if isinstance(data, list):
            for ev in data:
                if ev.get("slug"):
                    slugs.add(ev["slug"].lower())
                for m in ev.get("markets", []) or []:
                    if m.get("conditionId"):
                        condition_ids.add(m["conditionId"].lower())
    log(f"Deportes activos: {len(slugs)} eventos, {len(condition_ids)} mercados")
    return slugs, condition_ids


def is_sport_trade(trade, sport_slugs, sport_condition_ids):
    cond = (trade.get("conditionId") or "").lower()
    if cond and cond in sport_condition_ids:
        return True
    event_slug = (trade.get("eventSlug") or "").lower()
    if event_slug and event_slug in sport_slugs:
        return True
    text = f" {trade.get('title','')} {trade.get('eventSlug','')} {trade.get('slug','')} ".lower()
    return any(kw in text for kw in SPORT_KEYWORDS)


def get_recent_trades(wallet, cutoff_ts):
    trades = safe_get(f"{DATA_API}/trades", params={"user": wallet, "limit": 100})
    if not isinstance(trades, list):
        return []
    return [t for t in trades if normalize_ts(t.get("timestamp")) >= cutoff_ts]


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Telegram excepcion: {e}")


def format_alert(trade, trader_name, wallet, rank):
    side = "COMPRA" if str(trade.get("side", "")).upper() == "BUY" else "VENTA"
    size = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    usd = size * price
    outcome = trade.get("outcome", "?")
    title = trade.get("title", "Mercado desconocido")
    event_slug = trade.get("eventSlug") or trade.get("slug") or ""
    link = f"https://polymarket.com/event/{event_slug}" if event_slug else "https://polymarket.com"
    profile = f"https://polymarket.com/profile/{wallet}" if wallet else ""
    ts = datetime.fromtimestamp(normalize_ts(trade.get("timestamp")), tz=timezone.utc).strftime("%H:%M UTC")
    
    title_lower = title.lower()
    outcome_lower = outcome.lower()
    
    market_type = "Mercado"
    
    if "o/u" in title_lower or "over" in outcome_lower or "under" in outcome_lower:
        if "corner" in title_lower:
            ou_type = "Corners"
        elif "gol" in title_lower or "goal" in title_lower or ("vs" in title_lower and "o/u" in title_lower and "corner" not in title_lower):
            ou_type = "Goles"
        elif "point" in title_lower or "run" in title_lower or "score" in title_lower:
            ou_type = "Puntos"
        elif "card" in title_lower or "tarjeta" in title_lower:
            ou_type = "Tarjetas"
        else:
            ou_type = "Totales"
        
        match = re.search(r'O/U\s*([\d.]+)', title, re.IGNORECASE)
        if match:
            market_type = f"{outcome} ({ou_type} {match.group(1)})"
        else:
            market_type = f"{outcome} ({ou_type})"
            
    elif "moneyline" in title_lower or ("to win" in title_lower and "vs" in title_lower):
        market_type = "Moneyline"
    elif "spread" in title_lower:
        match = re.search(r'([\+\-]\d+\.?\d*)', title)
        if match:
            market_type = f"Spread {match.group(1)}"
        else:
            market_type = "Spread"
    elif "gol" in title_lower or "goal" in title_lower:
        market_type = "Goles"
    elif "corner" in title_lower:
        market_type = "Corners"
    elif "tarjeta" in title_lower or "card" in title_lower:
        market_type = "Tarjetas"
    
    return (
        f"TOP {rank} - {trader_name}\n"
        f"{side}: {outcome}\n"
        f"Tipo: {market_type}\n"
        f"{title}\n\n"
        f"Monto: ${usd:,.0f}\n"
        f"Precio: {price:.2f} ({price*100:.0f}%)\n"
        f"{ts}\n"
        f"Enlace: {link}"
    )


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        sys.exit(1)

    log(f"CONFIG: TOP_N={TOP_N}, MIN_USD={MIN_USD}, WINDOW_MINUTES={WINDOW_MINUTES}")
    cutoff_ts = int(time.time()) - WINDOW_MINUTES * 60

    traders = get_top_traders()
    for w in WALLETS_EXTRA:
        if w not in traders:
            traders[w] = (f"Seguido {w[:8]}", 0)

    if not traders:
        log("Sin traders para monitorear.")
        return

    sport_slugs, sport_condition_ids = get_sport_market_ids()

    alerts, seen = 0, set()
    for wallet, (name, rank) in traders.items():
        recent = get_recent_trades(wallet, cutoff_ts)
        for trade in recent:
            tx = trade.get("transactionHash") or f"{wallet}-{trade.get('timestamp')}-{trade.get('asset')}"
            if tx in seen:
                continue
            seen.add(tx)

            if not is_sport_trade(trade, sport_slugs, sport_condition_ids):
                continue

            usd = float(trade.get("size", 0)) * float(trade.get("price", 0))
            if usd < MIN_USD:
                continue

            trader_name = trade.get("pseudonym") or trade.get("name") or name
            send_telegram(format_alert(trade, trader_name, wallet, rank))
            alerts += 1
            time.sleep(1)
        time.sleep(0.3)

    log(f"Listo. {alerts} alertas enviadas de {len(traders)} traders.")


if __name__ == "__main__":
    main()
