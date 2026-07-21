"""
Monitor Polymarket Deportes v5.2 -> Telegram
Vigila el TOP N traders (default 5) de la categoria SPORTS y alerta TODAS sus
apuestas deportivas, sin filtro de monto ni de rango de probabilidad.
Con detalle de tipo de O/U (Goles, Corners, Puntos, etc).

Cambios v5.2:
- TOP_N ahora default 5 (antes 10)
- Se elimino el filtro de rango de probabilidad (MIN_PRICE/MAX_PRICE 60-80%).
  Ahora se alerta cualquier apuesta deportiva de los 5 traders monitoreados,
  sin importar el precio/probabilidad del mercado.

Cambios v5.1:
- TOP_N default 10 (antes 50)
- Se elimino el filtro por monto minimo (MIN_USD). Se alertan TODAS las apuestas
  deportivas de los traders monitoreados, sin importar el tamano.
- Se sigue mostrando el monto ($) en la alerta, solo que ya no se usa para filtrar.

Cambios v5.0:
- Migrado de lb-api.polymarket.com (viejo/deprecado) a data-api.polymarket.com/v1/leaderboard (oficial)
- El endpoint oficial tope 50 traders por request -> se pagina con offset para juntar TOP_N > 50
- Se usa category=SPORTS directamente en el leaderboard (antes se traia el TOP overall y se filtraba despues)
- Cache en disco de los mercados deportivos activos (evita golpear Gamma API en cada corrida)
- Persistencia en disco de trades ya alertados (evita alertas duplicadas entre corridas del cron)
"""

import os
import sys
import time
import re
import json
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WALLETS_EXTRA = [w.strip().lower() for w in os.environ.get("WALLETS_EXTRA", "").split(",") if w.strip()]
TOP_N = int(os.environ.get("TOP_N", "25"))
LEADERBOARD_WINDOW = os.environ.get("LEADERBOARD_WINDOW", "30d")  # 1d/7d/30d/all (se mapea abajo)
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "6"))

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sports-monitor/5.1)"}

LEADERBOARD_MAX_PER_CALL = 50  # limite duro del endpoint oficial
TRADES_MAX_PER_CALL = 100      # limite del endpoint /trades por request
TRADES_MAX_PAGES = 10          # tope de paginas para no quedar en loop con wallets muy activas

# Cache de mercados deportivos: se refresca solo si pasaron mas de N minutos
SPORT_CACHE_FILE = os.environ.get("SPORT_CACHE_FILE", "sport_cache.json")
SPORT_CACHE_TTL_MIN = int(os.environ.get("SPORT_CACHE_TTL_MIN", "30"))

# Persistencia de trades ya notificados (evita duplicados entre corridas)
SEEN_FILE = os.environ.get("SEEN_FILE", "seen_trades.json")
SEEN_RETENTION_MIN = max(WINDOW_MINUTES * 3, 30)  # cuanto guardamos hashes viejos

SPORT_TAGS = ["sports", "soccer", "football", "nba", "nfl", "mlb", "nhl", "tennis", "ufc", "mma", "boxing", "golf", "f1", "cricket", "esports"]

# Nota: se quitaron " vs ", " vs. " y " @ " de este listado (v5.1) porque como fallback
# generico generaban falsos positivos con mercados no deportivos que simplemente
# comparan dos entidades (ej. elecciones, premios, corporativos). El match por
# conditionId/eventSlug contra el cache de Gamma sigue siendo la via principal;
# estas keywords son solo un respaldo cuando ese cache no cubre el mercado.
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


def get_top_traders():
    """Trae el TOP_N de la categoria SPORTS paginando de a 50 (limite del endpoint oficial)."""
    traders = {}
    period = _map_window_to_period(LEADERBOARD_WINDOW)
    offset = 0
    rank_counter = 0

    while len(traders) < TOP_N:
        limit = min(LEADERBOARD_MAX_PER_CALL, TOP_N - len(traders))
        data = safe_get(
            f"{DATA_API}/v1/leaderboard",
            params={
                "category": "SPORTS",
                "timePeriod": period,
                "orderBy": "PNL",
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(data, list) or not data:
            break

        for t in data:
            wallet = (t.get("proxyWallet") or "").lower()
            name = t.get("userName") or (wallet[:8] if wallet else "?")
            if wallet and wallet not in traders:
                rank_counter += 1
                traders[wallet] = (name, rank_counter)

        if len(data) < limit:
            break  # ya no hay mas paginas
        offset += limit

    log(f"Leaderboard SPORTS ({period}): {len(traders)} traders obtenidos")
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
    """Cachea en disco los mercados deportivos activos para no golpear Gamma API en cada corrida."""
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

    _save_json_file(SPORT_CACHE_FILE, {
        "cached_at": now,
        "slugs": list(slugs),
        "condition_ids": list(condition_ids),
    })
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
    """
    Trae trades del wallet paginando con offset hasta cubrir toda la ventana
    (antes se pedia un unico limit=100, y wallets muy activos podian perder
    trades dentro de la ventana si superaban ese limite).
    Se corta cuando la pagina ya no trae datos dentro de la ventana,
    cuando la API devuelve menos del limite pedido (ultima pagina),
    o al llegar a TRADES_MAX_PAGES como salvaguarda.
    """
    collected = []
    offset = 0

    for _ in range(TRADES_MAX_PAGES):
        page = safe_get(
            f"{DATA_API}/trades",
            params={"user": wallet, "limit": TRADES_MAX_PER_CALL, "offset": offset},
        )
        if not isinstance(page, list) or not page:
            break

        in_window = [t for t in page if normalize_ts(t.get("timestamp")) >= cutoff_ts]
        collected.extend(in_window)

        oldest_ts = min((normalize_ts(t.get("timestamp")) for t in page), default=0)
        if len(page) < TRADES_MAX_PER_CALL or oldest_ts < cutoff_ts:
            break  # ultima pagina, o ya salimos de la ventana

        offset += TRADES_MAX_PER_CALL

    return collected


def load_seen(cutoff_ts):
    """Carga hashes ya notificados y descarta los mas viejos que la ventana de retencion."""
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


def format_alert(trade, trader_name, wallet, rank, effective_prob=None):
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
        f"Probabilidad: {prob_pct:.0f}% (precio {price:.2f})\n"
        f"{ts}\n"
        f"Enlace: {link}"
    )


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        sys.exit(1)

    log(f"CONFIG: TOP_N={TOP_N}, WINDOW_MINUTES={WINDOW_MINUTES}, sin filtro de monto ni de probabilidad")
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

    alerts = 0
    for wallet, (name, rank) in traders.items():
        recent = get_recent_trades(wallet, cutoff_ts)
        for trade in recent:
            tx = trade.get("transactionHash") or f"{wallet}-{trade.get('timestamp')}-{trade.get('asset')}"
            if tx in seen:
                continue

            if not is_sport_trade(trade, sport_slugs, sport_condition_ids):
                continue

            price = float(trade.get("price", 0))
            outcome_str = str(trade.get("outcome", "")).strip().lower()
            # El "price" que devuelve la API es la probabilidad implicita del lado "Yes".
            # Si el trader tomo "No", su probabilidad real de ganar es el complemento.
            effective_prob = (1 - price) if outcome_str == "no" else price

            # v5.2: ya no se filtra por rango de probabilidad. Se alertan todas
            # las apuestas deportivas del TOP_N, sin importar monto ni precio.

            trader_name = trade.get("pseudonym") or trade.get("name") or name
            send_telegram(format_alert(trade, trader_name, wallet, rank, effective_prob))
            seen[tx] = normalize_ts(trade.get("timestamp")) or now_ts
            alerts += 1
            time.sleep(1)
        time.sleep(0.3)

    save_seen(seen)
    log(f"Listo. {alerts} alertas enviadas de {len(traders)} traders.")


if __name__ == "__main__":
    main()
