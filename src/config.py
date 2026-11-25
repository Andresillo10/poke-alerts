"""Configuración centralizada, URLs verificadas y límites operativos."""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "state.json"
EVENTS_HISTORY_FILE = BASE_DIR / "data" / "events_history.json"
DASHBOARD_DATA_JS = BASE_DIR / "docs" / "events_data.js"
DEFAULT_OUTPUT_DIR = BASE_DIR / "out"

REQUEST_TIMEOUT_SEC = 15
TELEGRAM_SEND_DELAY_SEC = 1.2
MAX_ALERTS_PER_RUN = 4
MAX_FUTURE_NOTIFY_LEAD_DAYS = 0
MAX_POGO_CARDS_PER_EVENT_TELEGRAM = 1
MAX_BOOTSTRAP_EVENTS_PER_GAME = 1
HEARTBEAT_INTERVAL_DAYS = 14
TOPIC_DEDUP_TTL_DAYS = 21
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
    "ptcgpocket_gg_codes": "https://ptcgpocket.gg/codes/",
    "ptcgpocket_gg_wp_search": "https://ptcgpocket.gg/wp-json/wp/v2/posts",
    "ptcgpocket_gg_wp_categories": "7,1,8,11",
    "ptcgpocket_gg_tier_list": "https://ptcgpocket.gg/tier-list/",
    "scrapedduck_events_full": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.json",
    "scrapedduck_raids": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/raids.json",
    "pvpoke_gamemaster_pokemon": "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/gamemaster/pokemon.json",
    "pvpoke_gamemaster_moves": "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/gamemaster/moves.json",
    "pvpoke_rankings_1500": "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/rankings/all/overall/rankings-1500.json",
    "pvpoke_rankings_2500": "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/rankings/all/overall/rankings-2500.json",
    "pvpoke_rankings_mega_2500": "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/rankings/mega/overall/rankings-2500.json",
    "pvpoke_rankings_10000": "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/rankings/all/overall/rankings-10000.json",
    "pogoapi_fast_moves": "https://pogoapi.net/api/v1/fast_moves.json",
    "pogoapi_charged_moves": "https://pogoapi.net/api/v1/charged_moves.json",
    "pogo_redeem_base": "https://store.pokemongo.com/offer-redemption",
    "tcgp_redeem_url": "https://gift.pokemontcgpocket.com/",
}

GAME_LABELS = {
    "pogo": "POKÉMON GO",
    "tcgp": "POKÉMON TCG POCKET",
}

TELEGRAM_GAME_NAMES = {
    "pogo": "Pokémon GO",
    "tcgp": "Pokémon TCG Pocket",
}

KIND_LABELS = {
    "code": "CÓDIGO DE REGALO",
    "event": "EVENTO ACTIVO",
    "news": "NOTICIA OFICIAL",
}

TELEGRAM_KIND_HEADERS = {
    ("pogo", "code"): "🎁 ¡Nuevo código promocional!",
    ("pogo", "event"): "⚡ ¡Nuevo evento en camino!",
    ("pogo", "news"): "📢 Noticia oficial de Entrenador",
    ("tcgp", "code"): "🎁 ¡Nuevo código de regalo!",
    ("tcgp", "event"): "🃏 ¡Evento especial en TCG Pocket!",
    ("tcgp", "news"): "📢 Novedad en TCG Pocket",
}
