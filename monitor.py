"""
Monitor Polymarket Deportes v4.2 → Telegram
Vigila los TOP 50 traders y alerta apuestas deportivas >= $500.
Con clasificación de tipo de mercado (O/U, Moneyline, Spread, Goles, Corners, etc).
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
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "4"))

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sports-monitor/4.2)"}

SPORT_TAGS = ["sports", "soccer", "football", "nba", "nfl", "mlb", "nhl",
              "tennis", "ufc", "mma", "boxing", "golf", "f1", "cricket", "esports"]

SPORT_KEYWORDS = [
    "soccer", "premier league", "epl", "la liga", "laliga", "serie a",
    "bundesliga", "ligue 1", "champions league", "ucl", "eur
