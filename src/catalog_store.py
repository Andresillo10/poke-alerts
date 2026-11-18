"""Almacén histórico unificado JSON + datos para Dashboard auto-actualizable."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.code_digest import format_validity_window, parse_date_flex
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
) -> str:
    """Determina estado actual ('activo', 'proximo' o 'finalizado') en hora Colombia."""
    s_dt = parse_date_flex(start_date)
    e_dt = parse_date_flex(end_date)
    if e_dt and e_dt < today_cot:
        return "finalizado"
    if s_dt and s_dt > today_cot:
        return "proximo"
    if kind == "news" and s_dt and (today_cot - s_dt).days > 14:
        return "finalizado"
    return "activo"


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
    return rewards_str[:380], list(dict.fromkeys(highlights))[:6]


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
    """Compara dos versiones del catálogo ignorando timestamps globales."""
    return old_doc.get("events") != new_doc.get("events") or old_doc.get("stats") != new_doc.get("stats")


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
            copied["status"] = _compute_status(
                kind=str(copied.get("kind") or "event"),
                start_date=copied.get("start_date"),
                end_date=copied.get("end_date"),
                today_cot=today_cot,
            )
            by_id[str(copied["id"])] = copied

    for rec in records_input:
        raw_item: AlertItem = rec["raw"]
        loc_item: AlertItem = rec["localized"]
        extra_ev: dict[str, Any] | None = rec.get("extra_ev")
        events_cache: dict[str, dict[str, Any]] = rec.get("events_cache") or {}
        tactical_summary: str = rec.get("tactical_summary") or ""
        search_filter: str = rec.get("search_filter") or ""

        cid = _canon_event_id(raw_item)
        start_iso, end_iso = _extract_dates_for_item(raw_item, extra_ev, events_cache)
        rewards_str, highlights = _extract_rewards_text(loc_item, extra_ev)

        prev = by_id.get(cid, {})
        first_seen = prev.get("first_seen_at") or now_iso
        merged_sources = list(dict.fromkeys([*(prev.get("sources") or []), raw_item.source_name]))
        merged_urls = list(dict.fromkeys([*(prev.get("urls") or []), raw_item.url]))
        merged_highlights = list(dict.fromkeys([*(prev.get("highlights") or []), *(highlights or [])]))[:6]

        final_start = prev.get("start_date") or start_iso
        final_end = prev.get("end_date") or end_iso
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
        )

        # Prioriza premios y textos más ricos al fusionar múltiples portales
        prev_rewards = str(prev.get("rewards") or "")
        new_is_rich = "Consultar detalle" not in rewards_str and (
            not prev_rewards or "Consultar detalle" in prev_rewards or len(rewards_str) > len(prev_rewards)
        )
        final_rewards = rewards_str if new_is_rich or not prev_rewards else prev_rewards
        keep_prev_text = bool(prev.get("title")) and not new_is_rich
        final_title = str(prev["title"]) if keep_prev_text else loc_item.title
        final_orig = str(prev.get("title_original") or raw_item.title)
        final_summary = str(prev.get("summary") or loc_item.summary) if keep_prev_text else (loc_item.summary or str(prev.get("summary") or ""))

        final_tactical = str(prev.get("tactical_summary") or tactical_summary)
        final_search = str(prev.get("search_filter") or search_filter or raw_item.promo_code or "")
        final_guide = str(prev.get("guide_url") or loc_item.deck_guide_url or raw_item.url)

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
            "sources": merged_sources,
            "urls": merged_urls,
            "first_seen_at": first_seen,
            "last_updated_at": prev.get("last_updated_at") or now_iso,
        }

        # Solo refresca last_updated_at cuando hay cambio real en campos de contenido
        comparable_prev = {k: v for k, v in prev.items() if k != "last_updated_at"}
        comparable_new = {k: v for k, v in new_entry.items() if k != "last_updated_at"}
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
