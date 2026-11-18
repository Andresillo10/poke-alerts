"""Orquestador principal de poke-alerts (scraping, traducción, tarjeta PNG, QR mazos y Telegram)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path

from src.card_renderer import render_alert_card
from src.config import DEFAULT_OUTPUT_DIR, MAX_ALERTS_PER_RUN, STATE_FILE, TELEGRAM_SEND_DELAY_SEC
from src.deck_qr import (
    CardCatalog,
    build_three_decks_for_event,
    extract_event_constraints,
    load_catalog,
)
from src.deck_sheet_renderer import render_deck_sheet
from src.enricher import enrich_alert_item
from src.models import AlertItem
from src.scrapers import create_session, fetch_all_sources, fetch_text_safe
from src.state import (
    load_state,
    mark_items_processed,
    save_state,
    select_pending_alerts,
    update_source_health_and_heartbeat,
)
from src.telegram_client import (
    build_deck_sheet_caption,
    build_telegram_caption,
    send_telegram_deck_sheet,
    send_telegram_photo,
)
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
        "--preview-sample",
        action="store_true",
        help="Envía 1 código vigente y 1 evento de TCG Pocket como vista previa a Telegram sin tocar state.json.",
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


def _select_preview_sample(items: list[AlertItem]) -> list[AlertItem]:
    """Elige 1 código vigente y 1 evento de TCG Pocket para probar el diseño en vivo."""
    sample: list[AlertItem] = []
    first_code = next((it for it in items if it.kind == "code"), None)
    first_tcgp = next((it for it in items if it.game == "tcgp"), None)
    if first_code:
        sample.append(first_code)
    if first_tcgp:
        sample.append(first_tcgp)
    if not sample and items:
        sample.append(items[0])
    return sample


def _maybe_build_tcgp_deck_sheet(
    *,
    session,
    raw_item: AlertItem,
    localized_item: AlertItem,
    out_dir: Path,
    catalog_holder: list[CardCatalog],
) -> tuple[list, Path] | None:
    """Genera lámina con 3 QR si el evento de TCG Pocket requiere combates."""
    if raw_item.game != "tcgp" or raw_item.kind != "event":
        return None
    try:
        if not catalog_holder:
            catalog_holder.append(load_catalog(session))
        catalog = catalog_holder[0]

        detail_html = ""
        try:
            detail_html = fetch_text_safe(session, raw_item.url)
        except Exception as exc:
            LOGGER.warning("[DeckSheet] Sin HTML detalle para %s (%s).", raw_item.url, exc)

        constraints = extract_event_constraints(
            title=raw_item.title,
            detail_html=detail_html,
            catalog=catalog,
            extra_text=" ".join(raw_item.guide_bullets),
        )
        if not constraints.is_combat_event:
            return None

        decks = build_three_decks_for_event(
            event_title=localized_item.title,
            event_summary=raw_item.summary + " " + " ".join(raw_item.guide_bullets),
            constraints=constraints,
            catalog=catalog,
        )
        if not decks:
            return None

        sheet_path = out_dir / f"decks_{_safe_filename(localized_item.uid)}"
        render_deck_sheet(localized_item.title, decks, sheet_path)
        return decks, sheet_path
    except Exception as exc:
        LOGGER.warning("[DeckSheet] No se pudo generar lámina QR para %s: %s", raw_item.uid, exc)
        return None


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

    if args.preview_sample:
        to_notify = _select_preview_sample(collected)
        to_seed_only: list[AlertItem] = []
        LOGGER.info("Modo preview-sample activo: enviando %d tarjetas de muestra.", len(to_notify))
    else:
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
    events_cache: dict[str, dict] = {}
    catalog_holder: list[CardCatalog] = []

    for idx, item in enumerate(to_notify):
        enriched = enrich_alert_item(session, item, events_cache=events_cache)
        localized = localize_alert_item(enriched, enable_remote=not args.no_translate)
        png_path = args.out_dir / _safe_filename(localized.uid)
        render_alert_card(localized, png_path)

        deck_result = _maybe_build_tcgp_deck_sheet(
            session=session,
            raw_item=enriched,
            localized_item=localized,
            out_dir=args.out_dir,
            catalog_holder=catalog_holder,
        )

        if args.dry_run:
            LOGGER.info("[DRY-RUN] PNG: %s", png_path)
            print("--- CAPTION TELEGRAM ---")
            print(build_telegram_caption(localized))
            print("------------------------")
            if deck_result:
                decks, sheet_path = deck_result
                LOGGER.info("[DRY-RUN] DECK SHEET QR: %s", sheet_path)
                print("--- CAPTION DECK QR ---")
                print(build_deck_sheet_caption(localized, decks))
                print("-----------------------")
            sent_items.append(item)
            continue

        send_telegram_photo(
            bot_token=bot_token,
            chat_id=chat_id,
            item=localized,
            image_path=png_path,
        )
        if deck_result:
            decks, sheet_path = deck_result
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
            send_telegram_deck_sheet(
                bot_token=bot_token,
                chat_id=chat_id,
                item=localized,
                decks=decks,
                sheet_path=sheet_path,
            )
        sent_items.append(item)
        if idx < len(to_notify) - 1:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)

    if args.dry_run or args.preview_sample:
        LOGGER.info("Modo prueba completado sin modificar state.json.")
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
