"""Orquestador principal de poke-alerts (scraping, traducción, tarjeta PNG, QR mazos y Telegram)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path

from src.card_renderer import render_alert_card
from src.catalog_store import (
    _canon_event_id,
    load_events_history,
    sync_events_catalog,
)
from src.code_digest import (
    build_active_code_entries,
    build_weekly_codes_caption,
    render_weekly_codes_card,
)
from src.config import (
    DASHBOARD_DATA_JS,
    DEFAULT_OUTPUT_DIR,
    EVENTS_HISTORY_FILE,
    MAX_ALERTS_PER_RUN,
    STATE_FILE,
    TELEGRAM_SEND_DELAY_SEC,
    URLS,
)
from src.deck_qr import (
    BuiltDeck,
    CardCatalog,
    build_three_decks_for_event,
    extract_event_constraints,
    load_catalog,
)
from src.deck_sheet_renderer import render_single_deck_card
from src.enricher import enrich_alert_item
from src.models import AlertItem
from src.pogo_intel import (
    PogoCatalog,
    PogoTacticalCard,
    build_pogo_event_cards,
    build_pogo_weekly_meta_report,
    load_pogo_catalog,
)
from src.pogo_sheet_renderer import render_pogo_tactical_card
from src.scrapers import create_session, fetch_all_sources, fetch_text_safe
from src.state import (
    load_state,
    mark_items_processed,
    save_state,
    select_pending_alerts,
    should_send_one_time_ping,
    should_send_weekly_codes,
    should_send_weekly_meta,
    update_source_health_and_heartbeat,
)
from src.telegram_client import (
    build_pogo_tactical_caption,
    build_single_deck_caption,
    build_telegram_caption,
    send_telegram_photo,
    send_telegram_pogo_card,
    send_telegram_single_deck,
    send_telegram_text,
    send_telegram_weekly_codes,
)
from src.translator import localize_alert_item
from src.weekly_meta import (
    WeeklyMetaReport,
    build_weekly_ranked_report,
    pick_champion_deck_for_slot,
)

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
    parser.add_argument(
        "--weekly-meta",
        action="store_true",
        help="Fuerza el Top 3 semanal de TCG Pocket y Pokémon GO.",
    )
    parser.add_argument(
        "--weekly-codes",
        action="store_true",
        help="Fuerza el consolidado semanal de códigos promocionales activos.",
    )
    parser.add_argument(
        "--sync-catalog-only",
        action="store_true",
        help="Actualiza data/events_history.json y docs/events_data.js sin enviar mensajes ni alterar state.json.",
    )
    return parser.parse_args()


def _prioritize_items(items: list[AlertItem]) -> list[AlertItem]:
    """Prioriza códigos promocionales por encima de eventos y noticias generales."""
    priority = {"code": 0, "event": 1, "news": 2}
    return sorted(items, key=lambda it: priority.get(it.kind, 9))


def _select_preview_sample(items: list[AlertItem]) -> list[AlertItem]:
    """Elige 1 código, 1 evento TCG Pocket y 1 evento Pokémon GO para muestra integral."""
    sample: list[AlertItem] = []
    first_code = next((it for it in items if it.kind == "code"), None)
    first_tcgp = next((it for it in items if it.game == "tcgp" and it.kind == "event"), None) or next(
        (it for it in items if it.game == "tcgp"), None
    )
    first_pogo = next(
        (
            it
            for it in items
            if it.game == "pogo"
            and it.kind == "event"
            and any(k in it.title.lower() for k in ("raid hour", "raid day", "community day", "spotlight hour", "great league", "ultra league", "raid battles"))
        ),
        None,
    ) or next((it for it in items if it.game == "pogo" and it.kind == "event"), None)
    for pick in (first_code, first_tcgp, first_pogo):
        if pick:
            sample.append(pick)
    if not sample and items:
        sample.append(items[0])
    return sample


def _maybe_build_pogo_cards(
    *,
    session,
    raw_item: AlertItem,
    localized_item: AlertItem,
    out_dir: Path,
    pogo_catalog_holder: list[PogoCatalog],
    events_cache: dict[str, dict],
) -> list[tuple[PogoTacticalCard, Path]]:
    """Genera hasta 3 tarjetas tácticas PNG (1080×720) para eventos accionables de Pokémon GO."""
    if raw_item.game != "pogo" or raw_item.kind == "code":
        return []
    try:
        if not pogo_catalog_holder:
            pogo_catalog_holder.append(load_pogo_catalog(session))
        pogo_cat = pogo_catalog_holder[0]
        ev_id = raw_item.uid.removeprefix("scrapedduck:")
        extra_ev = events_cache.get(ev_id)
        tactical_cards = build_pogo_event_cards(
            raw_item,
            extra_ev,
            pogo_cat,
            context_title=localized_item.title,
        )
        if not tactical_cards:
            return []

        base_name = _safe_filename(localized_item.uid).removesuffix(".png")
        rendered: list[tuple[PogoTacticalCard, Path]] = []
        for idx, card in enumerate(tactical_cards, 1):
            card_path = out_dir / f"pogo_{base_name}_{idx}.png"
            render_pogo_tactical_card(card, card_path)
            rendered.append((card, card_path))
        return rendered
    except Exception as exc:
        LOGGER.warning("[PogoCards] Sin tarjetas tácticas para %s: %s", raw_item.uid, exc)
        return []


def _maybe_build_tcgp_decks(
    *,
    session,
    raw_item: AlertItem,
    localized_item: AlertItem,
    out_dir: Path,
    catalog_holder: list[CardCatalog],
    weekly_cache: list[WeeklyMetaReport | None],
) -> list[tuple[BuiltDeck, Path, str | None]]:
    """Genera 3 tarjetas individuales (1 QR cada una), usando campeones reales si aplica."""
    if raw_item.game != "tcgp" or raw_item.kind != "event":
        return []
    try:
        if not catalog_holder:
            catalog_holder.append(load_catalog(session))
        catalog = catalog_holder[0]

        detail_html = ""
        try:
            detail_html = fetch_text_safe(session, raw_item.url)
        except Exception as exc:
            LOGGER.warning("[DeckCards] Sin HTML detalle para %s (%s).", raw_item.url, exc)

        constraints = extract_event_constraints(
            title=raw_item.title,
            detail_html=detail_html,
            catalog=catalog,
            extra_text=" ".join(raw_item.guide_bullets),
        )
        if not constraints.is_combat_event:
            return []

        decks = build_three_decks_for_event(
            event_title=localized_item.title,
            event_summary=raw_item.summary + " " + " ".join(raw_item.guide_bullets),
            constraints=constraints,
            catalog=catalog,
        )
        if not decks:
            return []

        champ_urls: list[str | None] = [None] * len(decks)
        # En eventos abiertos (sin set único), mejora las opciones libres con campeones reales
        if constraints.allowed_sets is None:
            if not weekly_cache:
                try:
                    weekly_cache.append(build_weekly_ranked_report(session, catalog, max_tournaments=6, top_n=6))
                except Exception as exc:
                    LOGGER.warning("[DeckCards] Sin datos Limitless para evento abierto: %s", exc)
                    weekly_cache.append(None)
            report = weekly_cache[0]
            used_champs: set[str] = set()
            slot_rules = [
                (0, "Opción 1", constraints.no_ex, False, None, not (constraints.no_ex or constraints.diamond_only or constraints.required_types)),
                (1, "Opción 2", False, constraints.require_mega_ex, None, True),
                (2, "Opción 3", False, False, constraints.min_set_cards, not constraints.min_set_cards),
            ]
            for idx_s, prefix, req_no_ex, req_mega, req_min_set, allow_champ in slot_rules:
                if not allow_champ or idx_s >= len(decks):
                    continue
                picked = pick_champion_deck_for_slot(
                    report,
                    constraints,
                    slot_prefix=prefix,
                    require_no_ex=req_no_ex,
                    require_mega=req_mega,
                    min_set=req_min_set,
                    exclude_names=used_champs,
                )
                if picked:
                    champ_deck, champ_url = picked
                    decks[idx_s] = champ_deck
                    champ_urls[idx_s] = champ_url
                    used_champs.add(champ_deck.title.split(": ", 1)[-1])

        base_name = _safe_filename(localized_item.uid).removesuffix(".png")
        built: list[tuple[BuiltDeck, Path, str | None]] = []
        for idx, (deck, c_url) in enumerate(zip(decks, champ_urls), 1):
            card_path = out_dir / f"deck_{base_name}_{idx}.png"
            render_single_deck_card(
                deck,
                card_path,
                context_label=f"Evento: {localized_item.title}",
                badge_label="CAMPEÓN TORNEO + EVENTO" if c_url else "EVENTO TCG POCKET",
            )
            built.append((deck, card_path, c_url))
        return built
    except Exception as exc:
        LOGGER.warning("[DeckCards] No se pudieron generar QR individuales para %s: %s", raw_item.uid, exc)
        return []


def _maybe_run_weekly_meta(
    *,
    args: argparse.Namespace,
    session,
    catalog_holder: list[CardCatalog],
    pogo_catalog_holder: list[PogoCatalog],
    week_key: str,
    bot_token: str,
    chat_id: str,
) -> bool:
    """Genera y envía el Top 3 Ranked semanal de TCG Pocket y las 3 guías semanales GO."""
    tcgp_sent = False
    pogo_sent = False

    try:
        if not catalog_holder:
            catalog_holder.append(load_catalog(session))
        report = build_weekly_ranked_report(session, catalog_holder[0])
        if report and report.archetypes:
            for idx, arch in enumerate(report.archetypes, 1):
                card_path = args.out_dir / f"weekly_meta_{week_key}_{idx}.png"
                render_single_deck_card(
                    arch.deck,
                    card_path,
                    context_label=f"Torneos: {report.window_label} • {report.analyzed_entries} listas",
                    badge_label="TOP RANKED SEMANAL",
                )
                info_url = arch.best_url or "https://play.limitlesstcg.com/tournaments/completed?game=POCKET"
                extra_note = f"{report.window_label} | {arch.best_note}"
                if args.dry_run:
                    LOGGER.info("[DRY-RUN] WEEKLY META TCGP %d: %s", idx, card_path)
                    print(f"--- CAPTION WEEKLY META TCGP #{idx} ---")
                    print(
                        build_single_deck_caption(
                            arch.deck,
                            context_title="Top 3 Semanal Ranked",
                            header_tag="🏆 <b>Mazo Ranked Semanal</b>",
                            extra_note=extra_note,
                        )
                    )
                    print("----------------------------------")
                    continue

                if idx > 1:
                    time.sleep(TELEGRAM_SEND_DELAY_SEC)
                send_telegram_single_deck(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    deck=arch.deck,
                    card_path=card_path,
                    context_title="Top 3 Semanal Ranked",
                    header_tag="🏆 <b>Mazo Ranked Semanal</b>",
                    info_label="🏆 Ver lista campeón",
                    info_url=info_url,
                    extra_note=extra_note,
                )
                tcgp_sent = True
        else:
            LOGGER.warning("[WeeklyMeta] Sin datos suficientes de torneos TCGP esta semana.")
    except Exception as exc:
        LOGGER.warning("[WeeklyMeta TCGP] Fallo no bloqueante: %s", exc)

    try:
        if not pogo_catalog_holder:
            pogo_catalog_holder.append(load_pogo_catalog(session))
        pogo_cards = build_pogo_weekly_meta_report(pogo_catalog_holder[0])
        for idx, p_card in enumerate(pogo_cards, 1):
            card_path = args.out_dir / f"pogo_weekly_meta_{week_key}_{idx}.png"
            render_pogo_tactical_card(p_card, card_path)
            if args.dry_run:
                LOGGER.info("[DRY-RUN] WEEKLY META POGO %d: %s", idx, card_path)
                print(f"--- CAPTION WEEKLY META POGO #{idx} ---")
                print(build_pogo_tactical_caption(p_card, header_tag="🏆 <b>Meta Semanal Pokémon GO</b>"))
                print("---------------------------------------")
                continue

            if tcgp_sent or idx > 1:
                time.sleep(TELEGRAM_SEND_DELAY_SEC)
            send_telegram_pogo_card(
                bot_token=bot_token,
                chat_id=chat_id,
                card=p_card,
                card_path=card_path,
                header_tag="🏆 <b>Meta Semanal Pokémon GO</b>",
            )
            pogo_sent = True
    except Exception as exc:
        LOGGER.warning("[WeeklyMeta POGO] Fallo no bloqueante: %s", exc)

    if args.dry_run:
        return False
    if tcgp_sent or pogo_sent:
        LOGGER.info("[WeeklyMeta] Enviado reporte semanal integral %s (TCGP=%s, POGO=%s).", week_key, tcgp_sent, pogo_sent)
    return tcgp_sent or pogo_sent


def _ensure_events_cache(session, events_cache: dict[str, dict]) -> None:
    """Garantiza que events.json completo esté cargado en caché cuando hace falta."""
    if events_cache:
        return
    try:
        resp = session.get(URLS["scrapedduck_events_full"], timeout=15)
        if resp.ok:
            for ev in resp.json():
                if ev.get("eventID"):
                    events_cache[ev["eventID"]] = ev
    except Exception as exc:
        LOGGER.warning("[Catalog] No se pudo cargar events.json completo: %s", exc)


def _maybe_run_weekly_codes(
    *,
    args: argparse.Namespace,
    session,
    collected: list[AlertItem],
    events_cache: dict[str, dict],
    state: dict,
    week_key: str,
    bot_token: str,
    chat_id: str,
) -> bool:
    """Genera y envía el consolidado semanal de códigos activos (lunes >=02:00 AM COT)."""
    try:
        _ensure_events_cache(session, events_cache)
        entries = build_active_code_entries(
            collected,
            events_cache,
            state,
            enable_remote=not args.no_translate,
        )
        card_path = args.out_dir / f"weekly_codes_{week_key}.png"
        render_weekly_codes_card(entries, card_path, week_label=week_key)
        if args.dry_run:
            LOGGER.info("[DRY-RUN] WEEKLY CODES PNG (%d): %s", len(entries), card_path)
            print("--- CAPTION WEEKLY CODES ---")
            print(build_weekly_codes_caption(entries, week_label=week_key))
            print("----------------------------")
            return False

        send_telegram_weekly_codes(
            bot_token=bot_token,
            chat_id=chat_id,
            entries=entries,
            card_path=card_path,
            week_label=week_key,
        )
        LOGGER.info("[WeeklyCodes] Consolidado enviado %s (%d códigos activos).", week_key, len(entries))
        return True
    except Exception as exc:
        LOGGER.warning("[WeeklyCodes] Fallo no bloqueante al enviar consolidado: %s", exc)
        return False


def _format_pogo_tactical_summary(cards: list[PogoTacticalCard]) -> str:
    """Resume tarjetas tácticas GO sin truncar palabras en mitad de frase."""
    parts = [f"{c.title} ({c.subtitle})" for c in cards[:2]]
    out = ""
    for p in parts:
        cand = p if not out else f"{out} • {p}"
        if len(cand) <= 240:
            out = cand
        else:
            break
    return out or (parts[0][:240].rsplit(" ", 1)[0] if parts else "")


def _sync_unified_catalog(
    *,
    session,
    collected: list[AlertItem],
    already_processed: dict[str, dict],
    events_cache: dict[str, dict],
    pogo_catalog_holder: list[PogoCatalog],
    enable_remote: bool,
) -> bool:
    """Actualiza data/events_history.json y docs/events_data.js con todos los eventos."""
    _ensure_events_cache(session, events_cache)
    existing = load_events_history(EVENTS_HISTORY_FILE)
    existing_by_id = {str(e.get("id")): e for e in (existing.get("events") or []) if e.get("id")}

    records_input: list[dict] = list(already_processed.values())
    processed_uids = {rec["raw"].uid for rec in records_input}

    for item in collected:
        if item.uid in processed_uids:
            continue
        cid = _canon_event_id(item)
        prev = existing_by_id.get(cid)
        ev_id = item.uid.removeprefix("scrapedduck:")
        extra_ev = events_cache.get(ev_id)

        # Evita re-descargar HTML cada hora para portales ya guardados en el catálogo
        already_has_source = bool(prev) and item.source_name in (prev.get("sources") or [])
        if prev and prev.get("title") and (already_has_source or (prev.get("rewards") and "Consultar detalle" not in prev["rewards"])):
            loc = AlertItem(
                uid=item.uid,
                game=item.game,
                kind=item.kind,
                title=str(prev["title"]),
                summary=str(prev.get("summary") or item.summary),
                url=item.url,
                source_name=item.source_name,
                promo_code=item.promo_code,
                redeem_url=item.redeem_url,
                rewards=item.rewards,
                expires_at=item.expires_at,
                language="es",
                guide_bullets=(),
                deck_guide_url=prev.get("guide_url"),
                starts_on=item.starts_on,
            )
            enriched = item
            tactical_summary = str(prev.get("tactical_summary") or "")
            search_filter = str(prev.get("search_filter") or "")
        else:
            enriched = enrich_alert_item(session, item, events_cache=events_cache)
            loc = localize_alert_item(enriched, enable_remote=enable_remote)
            tactical_summary = ""
            search_filter = ""
            if item.game == "pogo" and item.kind == "event":
                try:
                    if not pogo_catalog_holder:
                        pogo_catalog_holder.append(load_pogo_catalog(session))
                    from src.pogo_intel import build_pogo_event_cards

                    pcards = build_pogo_event_cards(enriched, extra_ev, pogo_catalog_holder[0], context_title=loc.title)
                    if pcards:
                        tactical_summary = _format_pogo_tactical_summary(pcards)
                        search_filter = pcards[0].search_string
                except Exception:
                    pass
            elif item.game == "tcgp" and loc.deck_guide_title:
                tactical_summary = f"Guía / Mazos: {loc.deck_guide_title}"

        records_input.append(
            {
                "raw": enriched,
                "localized": loc,
                "extra_ev": extra_ev,
                "events_cache": events_cache,
                "tactical_summary": tactical_summary,
                "search_filter": search_filter,
            }
        )

    doc, changed = sync_events_catalog(
        history_path=EVENTS_HISTORY_FILE,
        dashboard_js_path=DASHBOARD_DATA_JS,
        records_input=records_input,
    )
    LOGGER.info(
        "[Catalog] JSON unificado (%s) | total=%d | activos=%d | códigos=%d",
        "actualizado" if changed else "sin cambios",
        doc["stats"].get("total", 0),
        doc["stats"].get("active", 0),
        doc["stats"].get("codes_active", 0),
    )
    return changed


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

    if args.sync_catalog_only:
        to_notify = []
        to_seed_only = []
        need_weekly, week_key = False, ""
        need_codes, codes_week_key = False, ""
        need_ping, ping_tag = False, ""
    elif args.preview_sample:
        to_notify = _select_preview_sample(collected)
        to_seed_only: list[AlertItem] = []
        LOGGER.info("Modo preview-sample activo: enviando %d tarjetas de muestra.", len(to_notify))
        need_weekly, week_key = should_send_weekly_meta(state, force=args.weekly_meta)
        need_codes, codes_week_key = should_send_weekly_codes(state, force=args.weekly_codes)
        need_ping, ping_tag = False, ""
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
        need_weekly, week_key = should_send_weekly_meta(state, force=args.weekly_meta)
        need_codes, codes_week_key = should_send_weekly_codes(state, force=args.weekly_codes)
        need_ping, ping_tag = should_send_one_time_ping(state)
        if args.bootstrap_silent:
            need_weekly = args.weekly_meta
            need_codes = args.weekly_codes
            need_ping = False

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if (
        not args.dry_run
        and not args.sync_catalog_only
        and (to_notify or need_weekly or need_codes or need_ping)
        and (not bot_token or not chat_id)
    ):
        raise SystemExit(
            "Faltan variables TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID. "
            "Usa --dry-run para probar localmente o configúralas en GitHub Secrets."
        )

    sent_items: list[AlertItem] = []
    events_cache: dict[str, dict] = {}
    catalog_holder: list[CardCatalog] = []
    pogo_catalog_holder: list[PogoCatalog] = []
    weekly_cache: list[WeeklyMetaReport | None] = []
    processed_records: dict[str, dict] = {}

    for idx, item in enumerate(to_notify):
        enriched = enrich_alert_item(session, item, events_cache=events_cache)
        localized = localize_alert_item(enriched, enable_remote=not args.no_translate)
        png_path = args.out_dir / _safe_filename(localized.uid)
        render_alert_card(localized, png_path)

        deck_cards = _maybe_build_tcgp_decks(
            session=session,
            raw_item=enriched,
            localized_item=localized,
            out_dir=args.out_dir,
            catalog_holder=catalog_holder,
            weekly_cache=weekly_cache,
        )
        pogo_cards = _maybe_build_pogo_cards(
            session=session,
            raw_item=enriched,
            localized_item=localized,
            out_dir=args.out_dir,
            pogo_catalog_holder=pogo_catalog_holder,
            events_cache=events_cache,
        )
        ev_id = item.uid.removeprefix("scrapedduck:")
        tact_sum = ""
        srch_flt = ""
        if deck_cards:
            tact_sum = f"{len(deck_cards)} mazos QR: " + ", ".join(d.title.split(": ", 1)[-1] for d, _, _ in deck_cards)
        elif localized.deck_guide_title:
            tact_sum = f"Guía / Mazos: {localized.deck_guide_title}"
        elif pogo_cards:
            tact_sum = _format_pogo_tactical_summary([c for c, _ in pogo_cards])
            srch_flt = pogo_cards[0][0].search_string
        processed_records[item.uid] = {
            "raw": enriched,
            "localized": localized,
            "extra_ev": events_cache.get(ev_id),
            "events_cache": events_cache,
            "tactical_summary": tact_sum,
            "search_filter": srch_flt,
        }
        default_label = "🃏 Guía de mazos" if localized.deck_guide_url else "🔎 Ver evento"
        default_url = localized.deck_guide_url or localized.url

        if args.dry_run:
            LOGGER.info("[DRY-RUN] PNG: %s", png_path)
            print("--- CAPTION TELEGRAM ---")
            print(build_telegram_caption(localized))
            print("------------------------")
            for idx_d, (deck, card_path, _) in enumerate(deck_cards, 1):
                LOGGER.info("[DRY-RUN] DECK CARD %d QR: %s", idx_d, card_path)
                print(f"--- CAPTION DECK #{idx_d} ---")
                print(
                    build_single_deck_caption(
                        deck,
                        context_title=localized.title,
                        header_tag="🃏 <b>Mazo recomendado</b>",
                    )
                )
                print("-----------------------")
            for idx_p, (p_card, p_path) in enumerate(pogo_cards, 1):
                LOGGER.info("[DRY-RUN] POGO CARD %d: %s", idx_p, p_path)
                print(f"--- CAPTION POGO #{idx_p} ---")
                print(build_pogo_tactical_caption(p_card))
                print("-----------------------")
            sent_items.append(item)
            continue

        send_telegram_photo(
            bot_token=bot_token,
            chat_id=chat_id,
            item=localized,
            image_path=png_path,
        )
        for deck, card_path, champ_url in deck_cards:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
            send_telegram_single_deck(
                bot_token=bot_token,
                chat_id=chat_id,
                deck=deck,
                card_path=card_path,
                context_title=localized.title,
                header_tag="🃏 <b>Mazo recomendado</b>",
                info_label="🏆 Ver lista campeón" if champ_url else default_label,
                info_url=champ_url or default_url,
            )
        for p_card, p_path in pogo_cards:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
            send_telegram_pogo_card(
                bot_token=bot_token,
                chat_id=chat_id,
                card=p_card,
                card_path=p_path,
                fallback_url=localized.url,
            )
        sent_items.append(item)
        if idx < len(to_notify) - 1:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)

    weekly_sent = False
    if need_weekly:
        if to_notify and not args.dry_run:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
        weekly_sent = _maybe_run_weekly_meta(
            args=args,
            session=session,
            catalog_holder=catalog_holder,
            pogo_catalog_holder=pogo_catalog_holder,
            week_key=week_key,
            bot_token=bot_token,
            chat_id=chat_id,
        )

    codes_sent = False
    if need_codes:
        if (to_notify or weekly_sent) and not args.dry_run:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
        codes_sent = _maybe_run_weekly_codes(
            args=args,
            session=session,
            collected=collected,
            events_cache=events_cache,
            state=state,
            week_key=codes_week_key,
            bot_token=bot_token,
            chat_id=chat_id,
        )

    ping_sent = False
    if need_ping:
        ok_src = sum(1 for _, ok in source_statuses if ok)
        total_src = len(source_statuses)
        ping_text = (
            "🤖 <b>Disparo automático 09:23 AM OK</b>\n"
            f"✅ Reloj cron-job.org y GitHub Actions operando.\n"
            f"📡 Fuentes activas: <b>{ok_src}/{total_src}</b> sanas.\n"
            "🔕 <i>Mensaje único de comprobación (mañana seguirá en silencio salvo novedades reales).</i>"
        )
        if args.dry_run:
            LOGGER.info("[DRY-RUN] ONE-TIME PING:\n%s", ping_text)
        else:
            if to_notify or weekly_sent or codes_sent:
                time.sleep(TELEGRAM_SEND_DELAY_SEC)
            send_telegram_text(bot_token=bot_token, chat_id=chat_id, text=ping_text)
            ping_sent = True

    if args.sync_catalog_only:
        _sync_unified_catalog(
            session=session,
            collected=collected,
            already_processed=processed_records,
            events_cache=events_cache,
            pogo_catalog_holder=pogo_catalog_holder,
            enable_remote=not args.no_translate,
        )
        return 0

    if args.dry_run or args.preview_sample:
        LOGGER.info("Modo prueba completado sin modificar state.json.")
        return 0

    _sync_unified_catalog(
        session=session,
        collected=collected,
        already_processed=processed_records,
        events_cache=events_cache,
        pogo_catalog_holder=pogo_catalog_holder,
        enable_remote=not args.no_translate,
    )

    processed = sent_items + to_seed_only
    if processed:
        mark_items_processed(state, processed)
    if weekly_sent:
        state["last_weekly_meta_week"] = week_key
    if codes_sent:
        state["last_weekly_codes_week"] = codes_week_key
    if ping_sent:
        state["one_time_ping_sent"] = ping_tag

    changed = update_source_health_and_heartbeat(
        state,
        source_statuses,
        had_state_change=bool(processed or weekly_sent or codes_sent or ping_sent),
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
