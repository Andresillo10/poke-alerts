"""Orquestador principal de poke-alerts (scraping, traducción, tarjeta PNG y Telegram)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path

from src.card_renderer import render_alert_card
from src.config import DEFAULT_OUTPUT_DIR, MAX_ALERTS_PER_RUN, STATE_FILE, TELEGRAM_SEND_DELAY_SEC
from src.models import AlertItem
from src.scrapers import create_session, fetch_all_sources
from src.state import (
    load_state,
    mark_items_processed,
    save_state,
    select_pending_alerts,
    update_source_health_and_heartbeat,
)
from src.telegram_client import build_telegram_caption, send_telegram_photo
from src.translator import localize_alert_item

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
LOGGER = logging.getLogger("poke-alerts")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_filename(uid: str) -> str:
    """Convierte un UID en nombre de archivo seguro para PNG temporales."""
    return _SAFE_NAME_RE.sub("_", uid)[:70].strip("_") + ".png"


def parse_args() -> argparse.Namespace:
    """Lee argumentos de línea de comandos para ejecución productiva o pruebas locales."""
    parser = argparse.ArgumentParser(description="Monitor horario de códigos y eventos Pokémon.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ejecuta scraping, traducción y PNG locales sin enviar a Telegram ni alterar state.json.",
    )
    parser.add_argument(
        "--bootstrap-silent",
        action="store_true",
        help="Marca el histórico inicial como visto sin enviar alertas de arranque.",
    )
    parser.add_argument(
        "--no-translate",
        action="store_true",
        help="Desactiva llamadas remotas de traducción y usa únicamente el glosario local.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_ALERTS_PER_RUN,
        help="Máximo de alertas a procesar por ejecución.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Carpeta donde se guardan las tarjetas PNG generadas.",
    )
    return parser.parse_args()


def _prioritize_items(items: list[AlertItem]) -> list[AlertItem]:
    """Prioriza códigos promocionales por encima de eventos y noticias generales."""
    priority = {"code": 0, "event": 1, "news": 2}
    return sorted(items, key=lambda it: priority.get(it.kind, 9))


def run_pipeline(args: argparse.Namespace) -> int:
    """Ejecuta el ciclo completo de detección, renderizado y notificación."""
    state = load_state(STATE_FILE)
    session = create_session()
    results = fetch_all_sources(session)

    source_statuses: list[tuple[str, bool]] = []
    collected: list[AlertItem] = []
    for res in results:
        source_statuses.append((res.source_name, res.ok))
        LOGGER.info(
            "Fuente %-22s | ok=%-5s | items=%d%s",
            res.source_name,
            res.ok,
            len(res.items),
            f" | err={res.error}" if res.error else "",
        )
        collected.extend(res.items)

    collected = _prioritize_items(collected)
    to_notify, to_seed_only = select_pending_alerts(
        state,
        collected,
        max_alerts=args.limit,
        bootstrap_silent=args.bootstrap_silent,
    )
    LOGGER.info(
        "Pendientes por notificar: %d | Backlog a sembrar silenciosamente: %d",
        len(to_notify),
        len(to_seed_only),
    )

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and to_notify and (not bot_token or not chat_id):
        raise SystemExit(
            "Faltan variables TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID. "
            "Usa --dry-run para probar localmente o configúralas en GitHub Secrets."
        )

    sent_items: list[AlertItem] = []
    for idx, item in enumerate(to_notify):
        localized = localize_alert_item(item, enable_remote=not args.no_translate)
        png_path = args.out_dir / _safe_filename(localized.uid)
        render_alert_card(localized, png_path)

        if args.dry_run:
            LOGGER.info("[DRY-RUN] PNG: %s", png_path)
            print("--- CAPTION TELEGRAM ---")
            print(build_telegram_caption(localized))
            print("------------------------")
            sent_items.append(item)
            continue

        send_telegram_photo(
            bot_token=bot_token,
            chat_id=chat_id,
            item=localized,
            image_path=png_path,
        )
        sent_items.append(item)
        if idx < len(to_notify) - 1:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)

    if args.dry_run:
        LOGGER.info("Dry-run completado sin modificar state.json.")
        return 0

    processed = sent_items + to_seed_only
    if processed:
        mark_items_processed(state, processed)

    changed = update_source_health_and_heartbeat(
        state,
        source_statuses,
        had_state_change=bool(processed),
    )
    if changed or processed:
        save_state(state, STATE_FILE)
        LOGGER.info("Estado guardado en %s (procesados=%d).", STATE_FILE, len(processed))
    else:
        LOGGER.info("Sin novedades ni heartbeat pendiente; state.json sin cambios.")
    return 0


def main() -> int:
    """Punto de entrada CLI."""
    return run_pipeline(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
