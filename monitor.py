"""
Monitor Polymarket Deportes v6.0 -> Telegram
Cambios respecto a v5.2 (foco: reducir ruido / falsas señales):

1. REINTRODUCE filtro de monto minimo (MIN_USD). En v5.1 se elimino por completo,
   lo cual hacia que se alertara CUALQUIER trade, incluso de $5, de los wallets
   seguidos. Default: 250 USD (ajustable por env var).

2. REINTRODUCE filtro de rango de probabilidad (MIN_PRICE/MAX_PRICE), pero
   DESACTIVADO por default (poner MIN_PRICE/MAX_PRICE en el env para activarlo).
   La razon de dejarlo disponible: apostar en un mercado ya resuelto al 95%+
   de probabilidad no es una señal util, sea quien sea el trader.

3. NUEVO: deteccion de consenso. Si 2+ wallets distintos del TOP_N operan el
   mismo mercado+outcome dentro de la misma corrida, se marca la alerta con
   "CONSENSO (Nx)". Esto es mucho mas fuerte como señal que un trade aislado,
   sin importar el tamaño individual.

4. NUEVO: soporte opcional para rankear por ROI en vez de PNL crudo, si el
   endpoint de leaderboard devuelve un campo de volumen utilizable. OJO: no
   se confirmo el nombre exacto de ese campo contra la API real -> revisar
   la respuesta cruda (ver funcion debug_print_leaderboard_sample) y ajustar
   LEADERBOARD_VOLUME_FIELD segun corresponda antes de confiar en esto.

Todo lo demas (paginacion, cache de mercados deportivos, persistencia de
trades vistos) se mantiene igual que en v5.2.
"""

import os
import sys
import time
import re
import json
import requests
from datetime import datetime, timezone
from collections import defaultdict

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WALLETS_EXTRA = [w.strip().lower() for w in os.environ.get("WALLETS_EXTRA", "").split(",") if w.strip()]
TOP_N = int(os.environ.get("TOP_N", "25"))
LEADERBOARD_WINDOW = os.environ.get("LEADERBOARD_WINDOW", "30d")  # 1d/7d/30d/all
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "6"))

# --- Filtros reintroducidos (v6.0) ---
MIN_USD = float(os.environ.get("MIN_USD", "250"))  # 0 para desactivar
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0"))    # 0 = sin piso
MAX_PRICE = float(os.environ.get("MAX_PRICE", "1"))    # 1 = sin techo
MIN_TRADES_FOR_RANK = int(os.environ.get("MIN_TRADES_FOR_RANK", "0"))  # requiere que el leaderboard traiga un campo de conteo de trades; 0 = desactivado
RANK_BY_ROI = os.environ.get("RANK_BY_ROI", "false").lower() == "true"
LEADERBOARD_VOLUME_FIELD = os.environ.get("LEADERBOARD_VOLUME_FIELD", "volume")  # VERIFICAR contra la API real

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sports-monitor/6.0)"}

LEADERBOARD_MAX_PER_CALL = 50
TRADES_MAX_PER_CALL = 100
TRADES_MAX_PAGES = 10

SPORT_CACHE_FILE = os.environ.get("SPORT_CACHE_FILE", "sport_cache.json")
SPORT_CACHE_TTL_MIN = int(os.environ.get("SPORT_CACHE_TTL_MIN", "30"))

SEEN_FILE = os.environ.get("SEEN_FILE", "seen_trades.json")
SEEN_RETENTION_MIN = max(WINDOW_MINUTES * 3, 30)

SPORT_TAGS = ["sports", "soccer", "football", "nba", "nfl", "mlb", "nhl", "tennis", "ufc", "mma", "boxing", "golf", "f1", "cricket", "esports"]

SPORT_KEYWORDS = ["soccer", "premier league", "epl", "la liga", "laliga", "serie a", "bundesliga", "ligue 1", "champions league", "ucl", "europa league", "world cup", "copa america", "copa libertadores", "fifa", "uefa", "mls", "liga mx", "fa cup", "golden boot", "top scorer", "nba", "nfl", "mlb", "nhl", "super bowl", "world series", "stanley cup", "finals", "playoffs", "grand slam", "wimbledon", "us open", "roland garros", "australian open", "ufc", "mma", "boxing", "heavyweight", "grand prix", "formula 1", "f1 ", "premier padel", "olympics", "ncaa", "march madness", "masters", "pga", "ryder cup", "cricket", "ipl", "rugby", "atp", "wta"]


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


def safe_get(url, params=None, timeout=25, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 2 * (attempt + 1)
                log(f"429 rate-limit en {url}, esperando {wait}s")
                time.sleep(wait)
                continue
            log(f"HTTP {r.status_code} en {url}")
            return None
        except Exception as e:
            log(f"Error en {url}: {e}")
            if attempt < retries:
                time.sleep(1.5)
    return None


def _map_window_to_period(window):
    w = (window or "").strip().lower()
    if w in ("1d", "day", "today"):
        return "DAY"
    if w in ("7d", "week"):
        return "WEEK"
    if w in ("30d", "month"):
        return "MONTH"
    return "ALL"


def debug_print_leaderboard_sample():
    """Corre esto una vez suelto (python -c 'import monitor; monitor.debug_print_leaderboard_sample()')
    para ver los campos reales que devuelve el leaderboard y ajustar LEADERBOARD_VOLUME_FIELD
    y el campo de conteo de trades si querés usar MIN_TRADES_FOR_RANK o RANK_BY_ROI."""
    data = safe_get(f"{DATA_API}/v1/leaderboard", params={"category": "SPORTS", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 3, "offset": 0})
    print(json.dumps(data, indent=2))


def get_top_traders():
    """Trae el TOP_N de la categoria SPORTS. Si RANK_BY_ROI esta activo, pide un
    universo mas grande (hasta 4x TOP_N) y re-rankea localmente por pnl/volumen
    antes de quedarse con los TOP_N finales. Esto evita que un solo trade
    suertudo domine el ranking."""
    period = _map_window_to_period(LEADERBOARD_WINDOW)
    pool_size = TOP_N * 4 if RANK_BY_ROI else TOP_N
    raw_entries = []
    offset = 0

    while len(raw_entries) < pool_size:
        limit = min(LEADERBOARD_MAX_PER_CALL, pool_size - len(raw_entries))
        data = safe_get(
            f"{DATA_API}/v1/leaderboard",
            params={"category": "SPORTS", "timePeriod": period, "orderBy": "PNL", "limit": limit, "offset": offset},
        )
        if not isinstance(data, list) or not data:
            break
        raw_entries.extend(data)
        if len(data) < limit:
            break
        offset += limit

    if MIN_TRADES_FOR_RANK > 0:
        before = len(raw_entries)
        raw_entries = [t for t in raw_entries if t.get("numTrades", t.get("trades", 0)) >= MIN_TRADES_FOR_RANK]
        log(f"Filtro MIN_TRADES_FOR_RANK: {before} -> {len(raw_entries)} (revisar nombre real del campo si esto queda en 0)")

    if RANK_BY_ROI:
        def roi_key(t):
            pnl = float(t.get("pnl", 0) or 0)
            vol = float(t.get(LEADERBOARD_VOLUME_FIELD, 0) or 0)
            return (pnl / vol) if vol > 0 else -999
        raw_entries.sort(key=roi_key, reverse=True)

    traders = {}
    for rank_counter, t in enumerate(raw_entries[:TOP_N], start=1):
        wallet = (t.get("proxyWallet") or "").lower()
        name = t.get("userName") or (wallet[:8] if wallet else "?")
        if wallet:
            traders[wallet] = (name, rank_counter)

    log(f"Leaderboard SPORTS ({period}): {len(traders)} traders finales (RANK_BY_ROI={RANK_BY_ROI})")
    return traders


def _load_json_file(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        log(f"No se pudo leer {path}: {e}")
    return default


def _save_json_file(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log(f"No se pudo guardar {path}: {e}")


def get_sport_market_ids():
    cache = _load_json_file(SPORT_CACHE_FILE, {})
    now = int(time.time())
    cached_at = cache.get("cached_at", 0)

    if cache.get("slugs") is not None and (now - cached_at) < SPORT_CACHE_TTL_MIN * 60:
        log(f"Mercados deportivos: usando cache ({len(cache['slugs'])} eventos, edad {(now - cached_at)//60}min)")
        return set(cache["slugs"]), set(cache["condition_ids"])

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

    _save_json_file(SPORT_CACHE_FILE, {"cached_at": now, "slugs": list(slugs), "condition_ids": list(condition_ids)})
    log(f"Deportes activos (refrescado): {len(slugs)} eventos, {len(condition_ids)} mercados")
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
    collected = []
    offset = 0
    for _ in range(TRADES_MAX_PAGES):
        page = safe_get(f"{DATA_API}/trades", params={"user": wallet, "limit": TRADES_MAX_PER_CALL, "offset": offset})
        if not isinstance(page, list) or not page:
            break
        in_window = [t for t in page if normalize_ts(t.get("timestamp")) >= cutoff_ts]
        collected.extend(in_window)
        oldest_ts = min((normalize_ts(t.get("timestamp")) for t in page), default=0)
        if len(page) < TRADES_MAX_PER_CALL or oldest_ts < cutoff_ts:
            break
        offset += TRADES_MAX_PER_CALL
    return collected


def load_seen(cutoff_ts):
    raw = _load_json_file(SEEN_FILE, {})
    retention_cutoff = int(time.time()) - SEEN_RETENTION_MIN * 60
    pruned = {tx: ts for tx, ts in raw.items() if ts >= retention_cutoff}
    log(f"Seen cargados: {len(pruned)} (de {len(raw)}, {len(raw)-len(pruned)} podados)")
    return pruned


def save_seen(seen_dict):
    _save_json_file(SEEN_FILE, seen_dict)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Telegram excepcion: {e}")


def format_alert(trade, trader_name, wallet, rank, effective_prob=None, consensus_count=1):
    side = "COMPRA" if str(trade.get("side", "")).upper() == "BUY" else "VENTA"
    size = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    usd = size * price
    outcome = trade.get("outcome", "?")
    title = trade.get("title", "Mercado desconocido")
    event_slug = trade.get("eventSlug") or trade.get("slug") or ""
    link = f"https://polymarket.com/event/{event_slug}" if event_slug else "https://polymarket.com"
    ts = datetime.fromtimestamp(normalize_ts(trade.get("timestamp")), tz=timezone.utc).strftime("%H:%M UTC")
    prob_pct = (effective_prob if effective_prob is not None else price) * 100

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
        market_type = f"{outcome} ({ou_type} {match.group(1)})" if match else f"{outcome} ({ou_type})"
    elif "moneyline" in title_lower or ("to win" in title_lower and "vs" in title_lower):
        market_type = "Moneyline"
    elif "spread" in title_lower:
        match = re.search(r'([\+\-]\d+\.?\d*)', title)
        market_type = f"Spread {match.group(1)}" if match else "Spread"
    elif "gol" in title_lower or "goal" in title_lower:
        market_type = "Goles"
    elif "corner" in title_lower:
        market_type = "Corners"
    elif "tarjeta" in title_lower or "card" in title_lower:
        market_type = "Tarjetas"

    consenso_tag = f"\U0001F525 CONSENSO ({consensus_count}x)\n" if consensus_count > 1 else ""

    return (
        f"{consenso_tag}"
        f"TOP {rank} - {trader_name}\n"
        f"{side}: {outcome}\n"
        f"Tipo: {market_type}\n"
        f"{title}\n\n"
        f"Monto: ${usd:,.0f}\n"
        f"Probabilidad: {prob_pct:.0f}% (precio {price:.2f})\n"
        f"{ts}\n"
        f"Enlace: {link}"
    )


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        sys.exit(1)

    log(f"CONFIG: TOP_N={TOP_N}, WINDOW_MINUTES={WINDOW_MINUTES}, MIN_USD={MIN_USD}, "
        f"MIN_PRICE={MIN_PRICE}, MAX_PRICE={MAX_PRICE}, RANK_BY_ROI={RANK_BY_ROI}")

    now_ts = int(time.time())
    cutoff_ts = now_ts - WINDOW_MINUTES * 60

    traders = get_top_traders()
    for w in WALLETS_EXTRA:
        if w not in traders:
            traders[w] = (f"Seguido {w[:8]}", 0)

    if not traders:
        log("Sin traders para monitorear.")
        return

    sport_slugs, sport_condition_ids = get_sport_market_ids()
    seen = load_seen(cutoff_ts)

    # Primero juntamos todos los trades candidatos (post-filtros) para poder
    # detectar consenso entre wallets antes de mandar nada a Telegram.
    candidate_trades = []  # lista de (tx, trade, trader_name, wallet, rank, effective_prob)
    market_groups = defaultdict(set)  # (conditionId, outcome) -> set de wallets

    for wallet, (name, rank) in traders.items():
        recent = get_recent_trades(wallet, cutoff_ts)
        for trade in recent:
            tx = trade.get("transactionHash") or f"{wallet}-{trade.get('timestamp')}-{trade.get('asset')}"
            if tx in seen:
                continue
            if not is_sport_trade(trade, sport_slugs, sport_condition_ids):
                continue

            price = float(trade.get("price", 0))
            size = float(trade.get("size", 0))
            usd = size * price
            outcome_str = str(trade.get("outcome", "")).strip().lower()
            effective_prob = (1 - price) if outcome_str == "no" else price

            # --- filtros reintroducidos ---
            if MIN_USD > 0 and usd < MIN_USD:
                continue
            if not (MIN_PRICE <= effective_prob <= MAX_PRICE):
                continue

            trader_name = trade.get("pseudonym") or trade.get("name") or name
            candidate_trades.append((tx, trade, trader_name, wallet, rank, effective_prob))

            group_key = (trade.get("conditionId", ""), outcome_str)
            market_groups[group_key].add(wallet)

        time.sleep(0.3)

    alerts = 0
    for tx, trade, trader_name, wallet, rank, effective_prob in candidate_trades:
        outcome_str = str(trade.get("outcome", "")).strip().lower()
        group_key = (trade.get("conditionId", ""), outcome_str)
        consensus_count = len(market_groups.get(group_key, {wallet}))

        send_telegram(format_alert(trade, trader_name, wallet, rank, effective_prob, consensus_count))
        seen[tx] = normalize_ts(trade.get("timestamp")) or now_ts
        alerts += 1
        time.sleep(1)

    save_seen(seen)
    log(f"Listo. {alerts} alertas enviadas de {len(traders)} traders (antes de filtros: revisar logs de get_recent_trades).")


if __name__ == "__main__":
    main()
              
