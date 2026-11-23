"""Almacén histórico unificado JSON + datos para Dashboard auto-actualizable."""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.code_digest import format_validity_window, parse_date_flex
from src.event_guide import (
    build_deck_proposal_intro,
    build_event_action_guide,
    build_search_aliases,
    classify_event_activities,
    extract_event_milestones,
)
from src.glossary import apply_post_translation_fixes
from src.models import AlertItem, build_topic_key, canonical_topic_signature
from src.scrapers import parse_serebii_end_date, parse_serebii_start_date

_COT_TZ = timezone(timedelta(hours=-5))


def _canon_event_id(item: AlertItem) -> str:
    """Calcula clave canónica estable para unificar portales sin duplicar eventos."""
    tkey = build_topic_key(item)
    return canonical_topic_signature(tkey) or tkey


def _classify_category(item: AlertItem, extra_ev: dict[str, Any] | None) -> str:
    """Asigna categoría legible para filtros del Dashboard."""
    if item.kind == "code":
        return "Código Promo"
    if item.kind == "news":
        return "Noticia Oficial"
    ev_type = str((extra_ev or {}).get("eventType") or "").lower()
    low = item.title.lower()
    if item.game == "tcgp":
        if any(k in low for k in ("battle", "combate", "drop", "botín", "emblem", "emblema")):
            return "Combate TCG Pocket"
        if any(k in low for k in ("wonder pick", "elección mágica", "outbreak", "aparición")):
            return "Elección / Aparición"
        if any(k in low for k in ("mission", "misiones", "gift", "regalo")):
            return "Misiones TCG Pocket"
        return "Evento TCG Pocket"
    if ev_type in ("raid-hour", "raid-day", "raid-battles") or "raid" in low or "incursi" in low:
        return "Incursiones GO"
    if ev_type in ("max-battles", "max-mondays") or "dynamax" in low or "gigantamax" in low:
        return "Combates Max"
    if ev_type == "community-day" or "community day" in low or "día de la comunidad" in low:
        return "Día de la Comunidad"
    if ev_type == "pokemon-spotlight-hour" or "spotlight" in low or "destacad" in low:
        return "Hora Destacada"
    if ev_type == "go-battle-league" or "battle league" in low or "liga" in low:
        return "Liga Combates GO"
    return "Evento Pokémon GO"


def _compute_status(
    *,
    kind: str,
    start_date: str | None,
    end_date: str | None,
    today_cot: date,
    first_seen_at: str | None = None,
) -> str:
    """Determina estado actual ('activo', 'proximo' o 'finalizado') en hora Colombia."""
    s_dt = parse_date_flex(start_date)
    e_dt = parse_date_flex(end_date)
    ref_dt = s_dt or parse_date_flex((first_seen_at or "")[:10])
    if e_dt and e_dt < today_cot:
        return "finalizado"
    if s_dt and s_dt > today_cot:
        return "proximo"
    if kind == "news" and ref_dt and (today_cot - ref_dt).days > 14:
        return "finalizado"
    # Eventos sin fecha de fin (p. ej. posts WordPress antiguos) vencen al cambiar de mes (>21 días)
    if (
        kind == "event"
        and not e_dt
        and ref_dt
        and (today_cot - ref_dt).days > 21
        and (ref_dt.year, ref_dt.month) < (today_cot.year, today_cot.month)
    ):
        return "finalizado"
    return "activo"


def _infer_published_at(row: dict[str, Any], fallback_iso: str) -> str:
    """Obtiene la fecha/hora exacta de publicación/notificación sin perder el histórico inicial."""
    if row.get("published_at"):
        return str(row["published_at"])
    first_seen = str(row.get("first_seen_at") or fallback_iso)
    last_upd = str(row.get("last_updated_at") or "")
    start_iso = str(row.get("start_date") or "")
    # Para eventos sembrados en el arranque inicial (18 nov) que luego dispararon alerta real de inicio
    if first_seen <= "2026-11-18T19:00:00Z" and last_upd > "2026-11-18T20:00:00Z" and start_iso:
        s_dt = parse_date_flex(start_iso)
        u_dt = parse_date_flex(last_upd[:10])
        if s_dt and u_dt and abs((u_dt - s_dt).days) <= 2:
            return last_upd
    return first_seen


def _extract_dates_for_item(
    item: AlertItem,
    extra_ev: dict[str, Any] | None,
    events_cache: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Extrae fechas ISO exactas (start_date, end_date) desde la mejor fuente disponible."""
    s_dt = parse_date_flex(item.starts_on)
    if item.source_name == "PTCGPocket.gg" and item.kind != "code":
        # En WordPress de PTCGPocket.gg el campo date representa fecha de publicación, no expiración
        s_dt = s_dt or parse_date_flex(item.expires_at)
        e_dt = None
    elif item.expires_at and " - " in item.expires_at:
        e_dt = parse_serebii_end_date(item.expires_at) or parse_date_flex(item.expires_at.split(" - ")[-1])
        s_dt = s_dt or parse_serebii_start_date(item.expires_at, end_dt=e_dt)
    else:
        e_dt = parse_date_flex(item.expires_at)

    if extra_ev:
        s_dt = parse_date_flex((extra_ev.get("start") or "")[:10]) or s_dt
        e_dt = parse_date_flex((extra_ev.get("end") or "")[:10]) or e_dt

    if item.kind == "code" and item.promo_code:
        c_up = item.promo_code.strip().upper()
        for ev in events_cache.values():
            codes = [str(c).strip().upper() for c in ((ev.get("extraData") or {}).get("promocodes") or [])]
            if c_up in codes:
                s_dt = parse_date_flex((ev.get("start") or "")[:10]) or s_dt
                e_dt = parse_date_flex((ev.get("end") or "")[:10]) or e_dt
                break

    if item.kind == "news" and not s_dt and e_dt:
        # En noticias WP/POGOLive, `expires_at` contiene fecha de publicación
        s_dt = e_dt
        e_dt = None

    return (
        s_dt.isoformat() if s_dt else None,
        e_dt.isoformat() if e_dt else None,
    )


def _extract_rewards_text(
    localized: AlertItem,
    extra_ev: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    """Extrae premios y puntos destacados legibles desde bullets y metadatos."""
    highlights = list(localized.guide_bullets)
    reward_parts: list[str] = []
    if localized.rewards:
        reward_parts.append(localized.rewards)

    for b in localized.guide_bullets:
        low = b.lower()
        if any(k in low for k in ("misiones", "cartas", "bonificaciones", "variocolor", "pokémon destacados")):
            reward_parts.append(b)

    if extra_ev:
        extra = extra_ev.get("extraData") or {}
        raids = (extra.get("raidbattles") or {}).get("bosses") or []
        if raids and not any("variocolor" in r.lower() for r in reward_parts):
            boss_txt = ", ".join(b["name"] for b in raids[:4] if b.get("name"))
            if boss_txt:
                highlights.append(f"Jefes: {boss_txt}")

    rewards_str = " | ".join(dict.fromkeys(p.strip() for p in reward_parts if p and p.strip()))
    if not rewards_str:
        rewards_str = "Consultar detalle oficial / misiones en juego"
    return rewards_str[:900], list(dict.fromkeys(highlights))[:8]


def load_events_history(path: Path) -> dict[str, Any]:
    """Carga el JSON unificado existente o inicializa estructura vacía."""
    if not path.exists():
        return {"schema_version": 1, "generated_at": "", "stats": {}, "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except Exception:
        pass
    return {"schema_version": 1, "generated_at": "", "stats": {}, "events": []}


def catalog_has_real_changes(old_doc: dict[str, Any], new_doc: dict[str, Any]) -> bool:
    """Compara dos versiones del catálogo ignorando timestamps de chequeo rutinario."""
    def _strip(ev: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in ev.items() if k not in ("decks_checked_at",)}

    old_evs = [_strip(e) for e in (old_doc.get("events") or []) if isinstance(e, dict)]
    new_evs = [_strip(e) for e in (new_doc.get("events") or []) if isinstance(e, dict)]
    return old_evs != new_evs or old_doc.get("stats") != new_doc.get("stats")


def sync_events_catalog(
    *,
    history_path: Path,
    dashboard_js_path: Path,
    records_input: list[dict[str, Any]],
    now_utc: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Fusiona eventos nuevos/actualizados en data/events_history.json y docs/events_data.js."""
    now = now_utc or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_cot = now.astimezone(_COT_TZ)
    today_cot = now_cot.date()

    existing_doc = load_events_history(history_path)
    by_id: dict[str, dict[str, Any]] = {}
    for row in existing_doc.get("events") or []:
        if isinstance(row, dict) and row.get("id"):
            copied = dict(row)
            for k in ("title", "summary", "rewards", "tactical_summary"):
                if isinstance(copied.get(k), str):
                    copied[k] = html.unescape(copied[k]).replace("Pok&émon", "Pokémon")
            if isinstance(copied.get("highlights"), list):
                copied["highlights"] = [html.unescape(str(h)).replace("Pok&émon", "Pokémon") for h in copied["highlights"]]
            copied["status"] = _compute_status(
                kind=str(copied.get("kind") or "event"),
                start_date=copied.get("start_date"),
                end_date=copied.get("end_date"),
                today_cot=today_cot,
                first_seen_at=copied.get("first_seen_at"),
            )
            copied["published_at"] = _infer_published_at(copied, now_iso)
            # Refresca campos pedagógicos estructurados en registros históricos existentes
            pseudo = AlertItem(
                uid=str(copied["id"]),
                game=copied.get("game", "pogo"),
                kind=copied.get("kind", "event"),
                title=str(copied.get("title") or ""),
                summary=str(copied.get("summary") or ""),
                url=((copied.get("urls") or [""])[0]),
                source_name=((copied.get("sources") or [""])[0]),
                promo_code=copied.get("promo_code"),
                redeem_url=copied.get("redeem_url"),
                rewards=copied.get("rewards"),
                guide_bullets=tuple(copied.get("highlights") or ()),
                starts_on=copied.get("start_date"),
            )
            ms_old = extract_event_milestones(pseudo, highlights=list(copied.get("highlights") or []))
            existing_decks = list(copied.get("recommended_decks") or [])
            req_b, acts_old = classify_event_activities(
                pseudo,
                milestones=ms_old,
                decks_count=0 if pseudo.game == "tcgp" else len(existing_decks),
            )
            if pseudo.game == "tcgp" and not req_b:
                existing_decks = []
                copied["recommended_decks"] = []
                if str(copied.get("tactical_summary") or "").startswith(("3 mazos QR:", "2 mazos QR:", "1 mazos QR:")):
                    copied["tactical_summary"] = ""
            copied["milestones"] = ms_old
            copied["requires_battles"] = req_b
            copied["activities"] = acts_old
            copied["action_guide"] = build_event_action_guide(
                pseudo,
                decks_count=len(existing_decks),
                pogo_cards_count=len(copied.get("tactical_cards") or []),
                milestones=ms_old,
                requires_battles=req_b,
                compact=False,
            )
            copied["deck_intro"] = build_deck_proposal_intro(
                pseudo,
                decks_count=len(existing_decks),
                decks_updated_label=str(copied.get("decks_updated_label") or ""),
            )
            by_id[str(copied["id"])] = copied

    for rec in records_input:
        raw_item: AlertItem = rec["raw"]
        loc_item: AlertItem = rec["localized"]
        extra_ev: dict[str, Any] | None = rec.get("extra_ev")
        events_cache: dict[str, dict[str, Any]] = rec.get("events_cache") or {}
        tactical_summary: str = rec.get("tactical_summary") or ""
        search_filter: str = rec.get("search_filter") or ""
        raw_rec_decks = rec.get("recommended_decks")
        raw_tactical_cards = rec.get("tactical_cards")

        cid = _canon_event_id(raw_item)
        start_iso, end_iso = _extract_dates_for_item(raw_item, extra_ev, events_cache)
        rewards_str, highlights = _extract_rewards_text(loc_item, extra_ev)

        prev = by_id.get(cid, {})
        first_seen = prev.get("first_seen_at") or now_iso
        merged_sources = list(dict.fromkeys([*(prev.get("sources") or []), raw_item.source_name]))
        merged_urls = list(dict.fromkeys([*(prev.get("urls") or []), raw_item.url]))
        raw_hl = list(dict.fromkeys([*(prev.get("highlights") or []), *(highlights or [])]))
        merged_highlights = [
            h for h in raw_hl
            if not any(bad in h.lower() for bad in ("mission type → reward", "day → mission type", "wins → reward"))
        ][:6]

        has_authoritative_dates = raw_item.source_name != "PTCGPocket.gg" and bool(raw_item.starts_on or extra_ev)
        final_start = (start_iso or prev.get("start_date")) if has_authoritative_dates else (prev.get("start_date") or start_iso)
        final_end = (end_iso or prev.get("end_date")) if has_authoritative_dates else (prev.get("end_date") or end_iso)
        raw_exp_for_lbl = None if (raw_item.source_name == "PTCGPocket.gg" or final_end) else raw_item.expires_at
        date_range_label = format_validity_window(
            final_start,
            final_end,
            detected_iso=first_seen[:10],
            raw_expires=raw_exp_for_lbl,
        )
        if not final_start and not final_end and not raw_exp_for_lbl and prev.get("date_range_label"):
            date_range_label = str(prev["date_range_label"])

        status = _compute_status(
            kind=raw_item.kind,
            start_date=final_start,
            end_date=final_end,
            today_cot=today_cot,
            first_seen_at=first_seen,
        )
        was_notified = bool(rec.get("was_notified"))
        published_at = now_iso if was_notified else _infer_published_at(prev, first_seen)

        # Prioriza premios y textos más ricos al fusionar múltiples portales
        prev_rewards = str(prev.get("rewards") or "")
        new_is_rich = "Consultar detalle" not in rewards_str and (
            not prev_rewards or "Consultar detalle" in prev_rewards or len(rewards_str) > len(prev_rewards)
        )
        final_rewards = apply_post_translation_fixes(rewards_str if new_is_rich or not prev_rewards else prev_rewards)
        keep_prev_text = bool(prev.get("title")) and not new_is_rich
        final_title = apply_post_translation_fixes(str(prev["title"]) if keep_prev_text else loc_item.title)
        final_orig = str(prev.get("title_original") or raw_item.title)
        raw_sum = str(prev.get("summary") or loc_item.summary) if keep_prev_text else (loc_item.summary or str(prev.get("summary") or ""))
        final_summary = apply_post_translation_fixes(raw_sum)
        merged_highlights = [apply_post_translation_fixes(h) for h in merged_highlights]

        prev_tactical = str(prev.get("tactical_summary") or "")
        force_replace_decks = bool(rec.get("force_replace_decks"))
        if force_replace_decks:
            final_tactical = tactical_summary
        elif tactical_summary and (
            not prev_tactical
            or (
                "mazos QR:" in tactical_summary
                and prev_tactical.startswith("Guía / Mazos:")
            )
        ):
            final_tactical = tactical_summary
        else:
            final_tactical = prev_tactical or tactical_summary
        final_search = str(prev.get("search_filter") or search_filter or raw_item.promo_code or "")
        final_guide = str(prev.get("guide_url") or loc_item.deck_guide_url or raw_item.url)
        if force_replace_decks and raw_rec_decks is not None:
            final_rec_decks = list(raw_rec_decks)
        else:
            final_rec_decks = (
                list(raw_rec_decks)
                if raw_rec_decks is not None and (len(raw_rec_decks) > 0 or not prev.get("recommended_decks"))
                else list(prev.get("recommended_decks") or [])
            )
        final_tactical_cards = (
            list(raw_tactical_cards)
            if raw_tactical_cards is not None and (len(raw_tactical_cards) > 0 or not prev.get("tactical_cards"))
            else list(prev.get("tactical_cards") or [])
        )
        enriched_loc = loc_item.with_spanish_copy(
            title=final_title,
            summary=final_summary,
            rewards=final_rewards,
            guide_bullets=tuple(merged_highlights),
        )
        milestones = extract_event_milestones(
            enriched_loc,
            extra_ev=extra_ev,
            highlights=merged_highlights,
        )
        requires_battles, activities = classify_event_activities(
            enriched_loc,
            extra_ev=extra_ev,
            milestones=milestones,
            decks_count=0 if (raw_item.game == "tcgp" and not force_replace_decks) else len(final_rec_decks),
        )
        if raw_item.game == "tcgp" and not requires_battles:
            final_rec_decks = []
            if final_tactical.startswith(("3 mazos QR:", "2 mazos QR:", "1 mazos QR:")):
                final_tactical = ""

        decks_checked_at = str(rec.get("decks_checked_at") or prev.get("decks_checked_at") or "")
        decks_updated_at = str(rec.get("decks_updated_at") or prev.get("decks_updated_at") or "")
        decks_updated_label = str(rec.get("decks_updated_label") or prev.get("decks_updated_label") or "")

        action_guide = build_event_action_guide(
            enriched_loc,
            extra_ev=extra_ev,
            decks_count=len(final_rec_decks),
            pogo_cards_count=len(final_tactical_cards),
            milestones=milestones,
            requires_battles=requires_battles,
            compact=False,
        )
        deck_intro = build_deck_proposal_intro(
            enriched_loc,
            decks_count=len(final_rec_decks),
            decks_updated_label=decks_updated_label,
        )
        search_aliases = build_search_aliases(raw_item, title_es=final_title)

        new_entry = {
            "id": cid,
            "game": raw_item.game,
            "game_label": "Pokémon GO" if raw_item.game == "pogo" else "Pokémon TCG Pocket",
            "kind": raw_item.kind,
            "kind_label": {"code": "Código", "event": "Evento", "news": "Noticia"}.get(raw_item.kind, "Evento"),
            "category": str(prev.get("category") or _classify_category(raw_item, extra_ev)),
            "title": final_title,
            "title_original": final_orig,
            "summary": final_summary,
            "action_guide": action_guide,
            "requires_battles": requires_battles,
            "activities": activities,
            "milestones": milestones,
            "deck_intro": deck_intro,
            "search_aliases": search_aliases,
            "rewards": final_rewards,
            "highlights": merged_highlights,
            "promo_code": raw_item.promo_code,
            "redeem_url": raw_item.redeem_url,
            "start_date": final_start,
            "end_date": final_end,
            "date_range_label": date_range_label,
            "status": status,
            "tactical_summary": final_tactical,
            "search_filter": final_search,
            "guide_url": final_guide,
            "recommended_decks": final_rec_decks,
            "tactical_cards": final_tactical_cards,
            "decks_checked_at": decks_checked_at,
            "decks_updated_at": decks_updated_at,
            "decks_updated_label": decks_updated_label,
            "sources": merged_sources,
            "urls": merged_urls,
            "published_at": published_at,
            "first_seen_at": first_seen,
            "last_updated_at": prev.get("last_updated_at") or now_iso,
        }

        # Solo refresca last_updated_at cuando hay cambio real en campos de contenido
        comparable_prev = {k: v for k, v in prev.items() if k not in ("last_updated_at", "decks_checked_at")}
        comparable_new = {k: v for k, v in new_entry.items() if k not in ("last_updated_at", "decks_checked_at")}
        if comparable_prev != comparable_new:
            new_entry["last_updated_at"] = now_iso
        by_id[cid] = new_entry

    status_rank = {"activo": 0, "proximo": 1, "finalizado": 2}
    ordered = sorted(
        by_id.values(),
        key=lambda e: (
            status_rank.get(str(e.get("status")), 9),
            0 if e.get("kind") == "code" else (1 if e.get("kind") == "event" else 2),
            e.get("end_date") or "9999-99-99",
            e.get("title") or "",
        ),
    )

    stats = {
        "total": len(ordered),
        "pogo": sum(1 for e in ordered if e.get("game") == "pogo"),
        "tcgp": sum(1 for e in ordered if e.get("game") == "tcgp"),
        "codes_active": sum(1 for e in ordered if e.get("kind") == "code" and e.get("status") == "activo"),
        "active": sum(1 for e in ordered if e.get("status") == "activo"),
        "upcoming": sum(1 for e in ordered if e.get("status") == "proximo"),
        "ended": sum(1 for e in ordered if e.get("status") == "finalizado"),
    }

    candidate_doc = {
        "schema_version": 1,
        "generated_at": existing_doc.get("generated_at") or now_cot.strftime("%Y-%m-%d %H:%M COT"),
        "stats": stats,
        "events": ordered,
    }

    changed = catalog_has_real_changes(existing_doc, candidate_doc) or not history_path.exists() or not dashboard_js_path.exists()
    if changed:
        candidate_doc["generated_at"] = now_cot.strftime("%Y-%m-%d %H:%M COT")
        history_path.parent.mkdir(parents=True, exist_ok=True)
        dashboard_js_path.parent.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(candidate_doc, indent=2, ensure_ascii=False) + "\n"
        history_path.write_text(json_text, encoding="utf-8")
        dashboard_js_path.write_text(
            "// Generado automáticamente por poke-alerts; no editar manualmente.\n"
            f"window.POKE_ALERTS_DB = {json_text.strip()};\n",
            encoding="utf-8",
        )
    return candidate_doc, changed
