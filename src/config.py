"""Configuración centralizada, URLs verificadas y límites operativos."""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "state.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "out"

REQUEST_TIMEOUT_SEC = 15
TELEGRAM_SEND_DELAY_SEC = 1.2
MAX_ALERTS_PER_RUN = 8
MAX_BOOTSTRAP_EVENTS_PER_GAME = 1
HEARTBEAT_INTERVAL_DAYS = 14
MAX_SEEN_ITEMS = 1500
TELEGRAM_CAPTION_LIMIT = 1024

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

URLS = {
    "leekduck_promo": "https://leekduck.com/promo-codes/",
    "scrapedduck_events": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json",
    "pogolive_es_news": "https://pokemongolive.com/es-MX/news",
    "serebii_pogo_events": "https://www.serebii.net/pokemongo/events.shtml",
    "serebii_tcgp_events": "https://www.serebii.net/tcgpocket/events.shtml",
    "ptcgpocket_gg_events": "https://ptcgpocket.gg/events/",
    "pogo_redeem_base": "https://store.pokemongo.com/offer-redemption",
    "tcgp_redeem_url": "https://gift.pokemontcgpocket.com/",
}

GAME_LABELS = {
    "pogo": "POKÉMON GO",
    "tcgp": "POKÉMON TCG POCKET",
}

KIND_LABELS = {
    "code": "CÓDIGO PROMOCIONAL",
    "event": "EVENTO",
    "news": "NOTICIA OFICIAL",
}
