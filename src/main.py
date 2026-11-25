"""Orquestador principal de poke-alerts (scraping, traducción, tarjeta PNG, QR mazos y Telegram)."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
    MAX_POGO_CARDS_PER_EVENT_TELEGRAM,
    STATE_FILE,
    TELEGRAM_SEND_DELAY_SEC,
    URLS,
)
from src.deck_qr import (
    ENERGY_ID_LABELS_ES,
    BuiltDeck,
    CardCatalog,
    build_qr_download_url,
    build_three_decks_for_event,
    describe_deck_strategy,
    extract_event_constraints,
    extract_ptcgpocket_event_decks,
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
    should_send_scheduled_deck_update,
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


def _resolve_pogo_cards(
    *,
    session,
    raw_item: AlertItem,
    localized_item: AlertItem,
    pogo_catalog_holder: list[PogoCatalog],
    events_cache: dict[str, dict],
    include_playbook: bool = False,
    allow_news: bool = False,
) -> list[PogoTacticalCard]:
    """Calcula en memoria hasta 3 tarjetas tácticas para eventos accionables de Pokémon GO."""
    if raw_item.game != "pogo" or raw_item.kind == "code":
        return []
    try:
        if not pogo_catalog_holder:
            pogo_catalog_holder.append(load_pogo_catalog(session))
        pogo_cat = pogo_catalog_holder[0]
        ev_id = raw_item.uid.removeprefix("scrapedduck:")
        extra_ev = events_cache.get(ev_id)
        return build_pogo_event_cards(
            raw_item,
            extra_ev,
            pogo_cat,
            context_title=localized_item.title,
            include_playbook=include_playbook,
            allow_news=allow_news,
        )
    except Exception as exc:
        LOGGER.warning("[PogoCards] Sin tarjetas tácticas para %s: %s", raw_item.uid, exc)
        return []


def _serialize_pogo_cards(cards: list[PogoTacticalCard]) -> list[dict]:
    """Serializa tarjetas tácticas GO a JSON para renderizarlas directo en el dashboard."""
    return [
        {
            "title": c.title,
            "subtitle": c.subtitle,
            "primary_type": c.primary_type,
            "badge_label": c.badge_label,
            "left_heading": c.left_heading,
            "left_rows": list(c.left_rows),
            "right_heading": c.right_heading,
            "right_rows": list(c.right_rows),
            "explanation": c.explanation,
            "search_label": c.search_label,
            "search_string": c.search_string,
            "info_label": c.info_label,
            "info_url": c.info_url,
            "context_label": c.context_label,
        }
        for c in cards
    ]


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
    tactical_cards = _resolve_pogo_cards(
        session=session,
        raw_item=raw_item,
        localized_item=localized_item,
        pogo_catalog_holder=pogo_catalog_holder,
        events_cache=events_cache,
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


def _build_urls_by_cid(collected: list[AlertItem]) -> dict[str, list[str]]:
    """Agrupa todas las URLs recopiladas entre portales por ID canónico de evento."""
    by_cid: dict[str, list[str]] = {}
    for item in collected:
        cid = _canon_event_id(item)
        urls = by_cid.setdefault(cid, [])
        if item.url and item.url not in urls:
            urls.append(item.url)
    return by_cid


def _deck_badge_label(source_url: str | None) -> str:
    """Devuelve la etiqueta honesta del origen real de la baraja."""
    if source_url and "limitlesstcg.com" in source_url:
        return "CAMPEÓN TORNEO + EVENTO"
    if source_url and "ptcgpocket.gg" in source_url:
        return "META EVENTO PTCGPOCKET"
    return "EVENTO TCG POCKET"


def _resolve_tcgp_decks(
    *,
    session,
    raw_item: AlertItem,
    localized_item: AlertItem,
    catalog_holder: list[CardCatalog],
    weekly_cache: list[WeeklyMetaReport | None],
    extra_urls: tuple[str, ...] | list[str] = (),
) -> list[tuple[BuiltDeck, str | None]]:
    """Calcula en memoria hasta 3 barajas válidas (curadas PTCGPocket/Limitless + sinergia)."""
    if raw_item.game != "tcgp" or raw_item.kind != "event":
        return []
    try:
        if not catalog_holder:
            catalog_holder.append(load_catalog(session))
        catalog = catalog_holder[0]

        candidate_urls: list[str] = []
        for u in [raw_item.url, *extra_urls]:
            if u and u not in candidate_urls:
                candidate_urls.append(u)

        html_by_url: dict[str, str] = {}
        for u in candidate_urls:
            try:
                html_by_url[u] = fetch_text_safe(session, u)
            except Exception as exc:
                LOGGER.warning("[DeckCards] Sin HTML detalle para %s (%s).", u, exc)

        detail_html = "\n".join(html_by_url.values())
        constraints = extract_event_constraints(
            title=raw_item.title,
            detail_html=detail_html,
            catalog=catalog,
            extra_text=" ".join(raw_item.guide_bullets),
        )
        if not constraints.is_combat_event:
            return []

        # Prioriza mazos curados reales publicados en guías de evento PTCGPocket.gg
        curated: list[tuple[BuiltDeck, str | None]] = []
        seen_signatures: set[tuple[str, ...]] = set()
        for u, page_html in html_by_url.items():
            if "ptcgpocket.gg" not in u.lower():
                continue
            for p_deck in extract_ptcgpocket_event_decks(
                session,
                detail_html=page_html,
                constraints=constraints,
                catalog=catalog,
                limit=3,
            ):
                sig = tuple(sorted(c.name for c in p_deck.cards if c.is_pokemon))
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                curated.append((p_deck, u))
                if len(curated) >= 3:
                    break
            if len(curated) >= 3:
                break

        decks: list[BuiltDeck] = [d for d, _ in curated]
        champ_urls: list[str | None] = [u for _, u in curated]

        if len(decks) < 3:
            algo_decks = build_three_decks_for_event(
                event_title=localized_item.title,
                event_summary=raw_item.summary + " " + " ".join(raw_item.guide_bullets),
                constraints=constraints,
                catalog=catalog,
            )
            for a_deck in algo_decks:
                if len(decks) >= 3:
                    break
                sig = tuple(sorted(c.name for c in a_deck.cards if c.is_pokemon))
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                idx = len(decks) + 1
                suffix = a_deck.title.split(": ", 1)[-1]
                decks.append(replace(a_deck, title=f"Opción {idx}: {suffix}"))
                champ_urls.append(None)

        if not decks:
            return []

        # En eventos abiertos (sin set único), mejora opciones sin origen externo con campeones reales
        if constraints.allowed_sets is None and any(u is None for u in champ_urls):
            if not weekly_cache:
                try:
                    weekly_cache.append(build_weekly_ranked_report(session, catalog, max_tournaments=6, top_n=6))
                except Exception as exc:
                    LOGGER.warning("[DeckCards] Sin datos Limitless para evento abierto: %s", exc)
                    weekly_cache.append(None)
            report = weekly_cache[0]
            used_champs: set[str] = {
                d.title.split(": ", 1)[-1] for d, cu in zip(decks, champ_urls) if cu
            }
            slot_rules = [
                (0, "Opción 1", constraints.no_ex, False, None, not (constraints.no_ex or constraints.diamond_only or constraints.required_types)),
                (1, "Opción 2", False, constraints.require_mega_ex, None, True),
                (2, "Opción 3", False, False, constraints.min_set_cards, not constraints.min_set_cards),
            ]
            for idx_s, prefix, req_no_ex, req_mega, req_min_set, allow_champ in slot_rules:
                if not allow_champ or idx_s >= len(decks) or champ_urls[idx_s] is not None:
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

        return list(zip(decks, champ_urls))
    except Exception as exc:
        LOGGER.warning("[DeckCards] No se pudieron calcular barajas para %s: %s", raw_item.uid, exc)
        return []


def _serialize_tcgp_decks(
    resolved: list[tuple[BuiltDeck, str | None]],
    *,
    fallback_url: str,
) -> list[dict]:
    """Serializa las barajas TCG Pocket recomendadas con sus cartas y QR de descarga."""
    items: list[dict] = []
    for deck, c_url in resolved:
        energy_label = "/".join(ENERGY_ID_LABELS_ES.get(e, "?") for e in deck.energy_types) or "Mixta"
        items.append(
            {
                "title": deck.title,
                "mission_tag": deck.mission_tag,
                "energy_label": energy_label,
                "strategy": describe_deck_strategy(deck),
                "cards": deck.summary_lines(),
                "qr_payload": deck.qr_payload,
                "qr_url": build_qr_download_url(deck.qr_payload),
                "champion_url": c_url if (c_url and "limitlesstcg.com" in c_url) else "",
                "source_url": c_url or fallback_url,
                "badge": _deck_badge_label(c_url),
            }
        )
    return items


def _maybe_build_tcgp_decks(
    *,
    session,
    raw_item: AlertItem,
    localized_item: AlertItem,
    out_dir: Path,
    catalog_holder: list[CardCatalog],
    weekly_cache: list[WeeklyMetaReport | None],
    extra_urls: tuple[str, ...] | list[str] = (),
) -> list[tuple[BuiltDeck, Path, str | None]]:
    """Genera 3 tarjetas individuales (1 QR cada una), usando meta real o sinergia."""
    resolved = _resolve_tcgp_decks(
        session=session,
        raw_item=raw_item,
        localized_item=localized_item,
        catalog_holder=catalog_holder,
        weekly_cache=weekly_cache,
        extra_urls=extra_urls,
    )
    if not resolved:
        return []
    base_name = _safe_filename(localized_item.uid).removesuffix(".png")
    built: list[tuple[BuiltDeck, Path, str | None]] = []
    for idx, (deck, c_url) in enumerate(resolved, 1):
        card_path = out_dir / f"deck_{base_name}_{idx}.png"
        render_single_deck_card(
            deck,
            card_path,
            context_label=f"Evento: {localized_item.title}",
            badge_label=_deck_badge_label(c_url),
        )
        built.append((deck, card_path, c_url))
    return built


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


_WR_TAG_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*WR", re.I)


def _deck_set_signature(decks: list[dict]) -> str:
    """Firma determinista basada en las 20 cartas de cada baraja para evitar ruido."""
    parts = ["|".join(sorted(str(c) for c in (d.get("cards") or []))) for d in decks]
    return " || ".join(parts)


def _deck_set_quality(decks: list[dict]) -> tuple[int, float, int]:
    """Puntúa objetivamente un set de barajas: origen (torneo/guía/motor), WR y cantidad."""
    tier_sum = 0
    wr_sum = 0.0
    for d in decks:
        champ = str(d.get("champion_url") or "").lower()
        src = str(d.get("source_url") or "").lower()
        if "limitlesstcg.com" in champ or "limitlesstcg.com" in src:
            tier_sum += 3
        elif "ptcgpocket.gg" in src and "/tier-list" not in src:
            tier_sum += 2
        else:
            tier_sum += 1
        m_wr = _WR_TAG_RE.search(str(d.get("mission_tag") or ""))
        if m_wr:
            wr_sum += float(m_wr.group(1))
    return (tier_sum, round(wr_sum, 2), len(decks))


def _is_strictly_better_deck_set(old_decks: list[dict], new_decks: list[dict]) -> bool:
    """Determina si el nuevo set mejora objetivamente al propuesto previamente para un evento activo."""
    if not new_decks:
        return False
    if not old_decks:
        return True
    if _deck_set_signature(old_decks) == _deck_set_signature(new_decks):
        return False
    old_q = _deck_set_quality(old_decks)
    new_q = _deck_set_quality(new_decks)
    if new_q > old_q:
        return True
    # Si el #1 cambió por un nuevo mazo del mismo tier con mayor WR o nueva guía dedicada
    if new_q[0] == old_q[0] and new_decks[0].get("cards") != old_decks[0].get("cards"):
        old_top_q = _deck_set_quality([old_decks[0]])
        new_top_q = _deck_set_quality([new_decks[0]])
        if new_top_q > old_top_q:
            return True
    return False


def _should_recheck_active_decks(prev: dict | None, now_utc: datetime, interval_hours: int = 6) -> bool:
    """Verifica si corresponde re-evaluar mazos de un evento TCGP activo."""
    if not prev:
        return True
    raw_ts = str(prev.get("decks_checked_at") or "").strip()
    if not raw_ts:
        return True
    try:
        last_dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now_utc - last_dt) >= timedelta(hours=interval_hours)
    except ValueError:
        return True


def _sync_unified_catalog(
    *,
    session,
    collected: list[AlertItem],
    already_processed: dict[str, dict],
    events_cache: dict[str, dict],
    pogo_catalog_holder: list[PogoCatalog],
    catalog_holder: list[CardCatalog],
    weekly_cache: list[WeeklyMetaReport | None],
    enable_remote: bool,
    state: dict | None = None,
    bot_token: str = "",
    chat_id: str = "",
    out_dir: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, bool]:
    """Actualiza data/events_history.json y docs/events_data.js y revisa mejores mazos en eventos activos."""
    _ensure_events_cache(session, events_cache)
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_cot = now_utc.astimezone(timezone(timedelta(hours=-5)))
    months_es = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    cot_label = f"{now_cot.day} {months_es[now_cot.month - 1]} · {now_cot.strftime('%H:%M')} COT"
    upgrade_notified = False
    existing = load_events_history(EVENTS_HISTORY_FILE)
    existing_by_id = {str(e.get("id")): e for e in (existing.get("events") or []) if e.get("id")}
    urls_by_cid = _build_urls_by_cid(collected)
    for eid, prev_ev in existing_by_id.items():
        merged = urls_by_cid.setdefault(eid, [])
        for u in prev_ev.get("urls") or []:
            if u and u not in merged:
                merged.append(u)

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
        needs_milestones = bool(
            item.game == "tcgp"
            and item.kind == "event"
            and prev
            and prev.get("status") == "activo"
            and "milestones" not in prev
        )
        has_basic_cache = bool(
            prev
            and prev.get("title")
            and not needs_milestones
            and (already_has_source or (prev.get("rewards") and "Consultar detalle" not in prev["rewards"]))
        )
        is_active_tcgp_event = bool(
            item.game == "tcgp"
            and item.kind == "event"
            and (not prev or prev.get("status") == "activo")
        )
        needs_decks = is_active_tcgp_event and (
            not prev
            or "recommended_decks" not in prev
            or _should_recheck_active_decks(prev, now_utc)
        )
        needs_pogo = item.game == "pogo" and item.kind != "code" and (not prev or not prev.get("tactical_cards"))

        force_replace_decks = False
        decks_checked_at = str(prev.get("decks_checked_at") or "") if prev else ""
        decks_updated_at = str(prev.get("decks_updated_at") or "") if prev else ""
        decks_updated_label = str(prev.get("decks_updated_label") or "") if prev else ""

        if has_basic_cache and not needs_decks and not needs_pogo:
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
            rec_decks = list(prev.get("recommended_decks") or [])
            tact_cards = list(prev.get("tactical_cards") or [])
        else:
            enriched = enrich_alert_item(session, item, events_cache=events_cache)
            loc = localize_alert_item(enriched, enable_remote=enable_remote and not has_basic_cache)
            if has_basic_cache and prev:
                loc = replace(
                    loc,
                    title=str(prev["title"]),
                    summary=str(prev.get("summary") or loc.summary),
                    deck_guide_url=prev.get("guide_url") or loc.deck_guide_url,
                )
            tactical_summary = str(prev.get("tactical_summary") or "") if prev else ""
            search_filter = str(prev.get("search_filter") or "") if prev else ""
            rec_decks = list(prev.get("recommended_decks") or []) if prev else []
            tact_cards = list(prev.get("tactical_cards") or []) if prev else []

            if item.game == "pogo" and item.kind != "code" and (not prev or needs_pogo or not tact_cards):
                pcards = _resolve_pogo_cards(
                    session=session,
                    raw_item=enriched,
                    localized_item=loc,
                    pogo_catalog_holder=pogo_catalog_holder,
                    events_cache=events_cache,
                    include_playbook=True,
                    allow_news=True,
                )
                if pcards:
                    tact_cards = _serialize_pogo_cards(pcards)
                    tactical_summary = _format_pogo_tactical_summary(pcards)
                    search_filter = pcards[0].search_string
            elif item.game == "tcgp" and item.kind == "event" and needs_decks:
                resolved_decks = _resolve_tcgp_decks(
                    session=session,
                    raw_item=enriched,
                    localized_item=loc,
                    catalog_holder=catalog_holder,
                    weekly_cache=weekly_cache,
                    extra_urls=urls_by_cid.get(cid, ()),
                )
                decks_checked_at = now_iso
                prev_decks = list(prev.get("recommended_decks") or []) if prev else []
                if not resolved_decks:
                    # El evento NO requiere combates: limpia cualquier baraja residual previa
                    if prev_decks:
                        force_replace_decks = True
                        rec_decks = []
                        tactical_summary = ""
                else:
                    candidate_decks = _serialize_tcgp_decks(
                        resolved_decks,
                        fallback_url=loc.deck_guide_url or loc.url,
                    )
                    if not prev_decks or _is_strictly_better_deck_set(prev_decks, candidate_decks):
                        had_prev = bool(prev_decks)
                        force_replace_decks = True
                        rec_decks = candidate_decks
                        tactical_summary = f"{len(rec_decks)} mazos QR: " + ", ".join(
                            d["title"].split(": ", 1)[-1] for d in rec_decks
                        )
                        if had_prev:
                            decks_updated_at = now_iso
                            decks_updated_label = cot_label
                            LOGGER.info(
                                "[DeckMonitor] Mejor mazo detectado para evento activo '%s': %s",
                                loc.title,
                                tactical_summary,
                            )
                            # Avisa 1 sola vez por Telegram (cooldown 24 h por evento) si hay mejora real
                            if (
                                state is not None
                                and bot_token
                                and chat_id
                                and out_dir is not None
                                and not dry_run
                            ):
                                upg_map = state.setdefault("deck_upgrade_notified", {})
                                last_upg = upg_map.get(cid) or {}
                                new_sig = _deck_set_signature(candidate_decks)
                                can_ping = last_upg.get("sig") != new_sig
                                if can_ping and last_upg.get("sent_at"):
                                    try:
                                        last_dt = datetime.fromisoformat(str(last_upg["sent_at"]).replace("Z", "+00:00"))
                                        if (now_utc - last_dt) < timedelta(hours=24):
                                            can_ping = False
                                    except ValueError:
                                        pass
                                if can_ping:
                                    top_deck, top_url = resolved_decks[0]
                                    try:
                                        card_path = out_dir / f"upgrade_{_safe_filename(cid)}"
                                        render_single_deck_card(
                                            top_deck,
                                            card_path,
                                            context_label=f"Mejor mazo encontrado • {loc.title}",
                                            badge_label="MEJOR MAZO ACTIVO",
                                        )
                                        send_telegram_single_deck(
                                            bot_token=bot_token,
                                            chat_id=chat_id,
                                            deck=top_deck,
                                            card_path=card_path,
                                            context_title=loc.title,
                                            header_tag="🔄 <b>¡Mejor mazo encontrado para el evento!</b>",
                                            info_label="🔎 Ver evento" if not top_url else "🏆 Ver fuente mazo",
                                            info_url=top_url or loc.url,
                                            extra_note=f"Evento activo: {loc.title} — actualizado en tu Dashboard",
                                        )
                                        upg_map[cid] = {"sig": new_sig, "sent_at": now_iso}
                                        upgrade_notified = True
                                    except Exception as exc:
                                        LOGGER.warning("[DeckMonitor] Fallo no bloqueante al avisar mejora: %s", exc)

        records_input.append(
            {
                "raw": enriched,
                "localized": loc,
                "extra_ev": extra_ev,
                "events_cache": events_cache,
                "tactical_summary": tactical_summary,
                "search_filter": search_filter,
                "recommended_decks": rec_decks,
                "tactical_cards": tact_cards,
                "force_replace_decks": force_replace_decks,
                "decks_checked_at": decks_checked_at,
                "decks_updated_at": decks_updated_at,
                "decks_updated_label": decks_updated_label,
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
    return changed, upgrade_notified


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
        need_deck_update, deck_update_tag = False, ""
    elif args.preview_sample:
        to_notify = _select_preview_sample(collected)
        to_seed_only: list[AlertItem] = []
        LOGGER.info("Modo preview-sample activo: enviando %d tarjetas de muestra.", len(to_notify))
        need_weekly, week_key = should_send_weekly_meta(state, force=args.weekly_meta)
        need_codes, codes_week_key = should_send_weekly_codes(state, force=args.weekly_codes)
        need_ping, ping_tag = False, ""
        need_deck_update, deck_update_tag = False, ""
    else:
        to_notify, to_seed_only = select_pending_alerts(
            state,
            collected,
            max_alerts=args.limit,
            bootstrap_silent=args.bootstrap_silent,
        )
        need_weekly, week_key = should_send_weekly_meta(state, force=args.weekly_meta)
        need_codes, codes_week_key = should_send_weekly_codes(state, force=args.weekly_codes)
        need_ping, ping_tag = should_send_one_time_ping(state)
        need_deck_update, deck_update_tag = should_send_scheduled_deck_update(state)
        if args.bootstrap_silent:
            need_weekly = args.weekly_meta
            need_codes = args.weekly_codes
            need_ping = False
            need_deck_update = False
        if need_deck_update:
            df_item = next(
                (
                    it for it in collected
                    if it.game == "tcgp"
                    and it.kind == "event"
                    and _canon_event_id(it) == "tcgp:battles-dual-fusion-limited"
                ),
                None,
            )
            if df_item and all(_canon_event_id(x) != "tcgp:battles-dual-fusion-limited" for x in to_notify):
                to_notify.insert(0, df_item)
                LOGGER.info("[DeckUpdate 20:00 COT] Encolada actualización de mazos para %s.", df_item.title)
        LOGGER.info(
            "Pendientes por notificar: %d | Backlog a sembrar silenciosamente: %d",
            len(to_notify),
            len(to_seed_only),
        )

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
    urls_by_cid = _build_urls_by_cid(collected)

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
            extra_urls=urls_by_cid.get(_canon_event_id(enriched), ()),
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
        default_label = "🃏 Guía de mazos" if localized.deck_guide_url else "🔎 Ver evento"
        default_url = localized.deck_guide_url or localized.url
        rec_decks = _serialize_tcgp_decks([(d, cu) for d, _, cu in deck_cards], fallback_url=default_url) if deck_cards else []
        dash_pogo_cards = [c for c, _ in pogo_cards]
        if item.game == "pogo" and item.kind != "code" and not dash_pogo_cards:
            dash_pogo_cards = _resolve_pogo_cards(
                session=session,
                raw_item=enriched,
                localized_item=localized,
                pogo_catalog_holder=pogo_catalog_holder,
                events_cache=events_cache,
                include_playbook=True,
                allow_news=True,
            )
            if dash_pogo_cards and not tact_sum:
                tact_sum = _format_pogo_tactical_summary(dash_pogo_cards)
                srch_flt = dash_pogo_cards[0].search_string
        tact_cards = _serialize_pogo_cards(dash_pogo_cards) if dash_pogo_cards else []
        processed_records[item.uid] = {
            "raw": enriched,
            "localized": localized,
            "extra_ev": events_cache.get(ev_id),
            "events_cache": events_cache,
            "tactical_summary": tact_sum,
            "search_filter": srch_flt,
            "recommended_decks": rec_decks,
            "tactical_cards": tact_cards,
        }

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

        try:
            send_telegram_photo(
                bot_token=bot_token,
                chat_id=chat_id,
                item=localized,
                image_path=png_path,
            )
        except Exception as exc:
            LOGGER.error("Fallo enviando alerta principal %s a Telegram: %s", localized.uid, exc)
            continue

        for deck, card_path, champ_url in deck_cards:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
            try:
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
            except Exception as exc:
                LOGGER.warning("Fallo no bloqueante enviando mazo %s: %s", deck.title, exc)
        for p_card, p_path in pogo_cards[:MAX_POGO_CARDS_PER_EVENT_TELEGRAM]:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
            try:
                send_telegram_pogo_card(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    card=p_card,
                    card_path=p_path,
                    fallback_url=localized.url,
                )
            except Exception as exc:
                LOGGER.warning("Fallo no bloqueante enviando tarjeta GO %s: %s", p_card.title, exc)
        sent_items.append(item)
        processed_records[item.uid]["was_notified"] = True
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
            catalog_holder=catalog_holder,
            weekly_cache=weekly_cache,
            enable_remote=not args.no_translate,
        )
        return 0

    if args.dry_run or args.preview_sample:
        LOGGER.info("Modo prueba completado sin modificar state.json.")
        return 0

    _, deck_upgrade_notified = _sync_unified_catalog(
        session=session,
        collected=collected,
        already_processed=processed_records,
        events_cache=events_cache,
        pogo_catalog_holder=pogo_catalog_holder,
        catalog_holder=catalog_holder,
        weekly_cache=weekly_cache,
        enable_remote=not args.no_translate,
        state=state,
        bot_token=bot_token,
        chat_id=chat_id,
        out_dir=args.out_dir,
        dry_run=args.dry_run,
    )

    processed = sent_items + to_seed_only
    if processed:
        mark_items_processed(state, processed, sent_uids={it.uid for it in sent_items})
    if weekly_sent:
        state["last_weekly_meta_week"] = week_key
    if codes_sent:
        state["last_weekly_codes_week"] = codes_week_key
    if ping_sent:
        state["one_time_ping_sent"] = ping_tag

    deck_update_done = False
    if need_deck_update and any(_canon_event_id(x) == "tcgp:battles-dual-fusion-limited" for x in sent_items):
        state["deck_update_sent"] = deck_update_tag
        deck_update_done = True

    changed = update_source_health_and_heartbeat(
        state,
        source_statuses,
        had_state_change=bool(processed or weekly_sent or codes_sent or ping_sent or deck_update_done or deck_upgrade_notified),
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
