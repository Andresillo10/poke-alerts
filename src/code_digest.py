"""Consolidado semanal de códigos promocionales activos (lunes 02:00 AM COT)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from src.card_renderer import _load_font, _wrap_text
from src.config import TELEGRAM_CAPTION_LIMIT
from src.models import AlertItem, format_date_es, parse_date_flex
from src.translator import translate_en_to_es

_COT_TZ = timezone(timedelta(hours=-5))
_CARD_W = 1080
_CARD_H = 720
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def format_validity_window(
    start_iso: str | None,
    end_iso: str | None,
    *,
    detected_iso: str | None = None,
    raw_expires: str | None = None,
) -> str:
    """Genera etiqueta honesta de vigencia ('desde -> hasta') sin inventar fechas."""
    s_dt = parse_date_flex(start_iso)
    e_dt = parse_date_flex(end_iso) or parse_date_flex(raw_expires)
    d_dt = parse_date_flex(detected_iso)

    if s_dt and e_dt:
        return f"{format_date_es(s_dt)} -> {format_date_es(e_dt)}"
    if s_dt and not e_dt:
        return f"Desde {format_date_es(s_dt)} (sin fecha fin publicada)"
    if e_dt and d_dt:
        return f"Detectado {format_date_es(d_dt)} -> Hasta {format_date_es(e_dt)}"
    if e_dt:
        return f"Válido hasta {format_date_es(e_dt)}"
    if raw_expires:
        return f"Vigencia: {raw_expires}"
    if d_dt:
        return f"Vigente (detectado desde {format_date_es(d_dt)})"
    return "Vigente actualmente (sin fecha límite publicada)"


@dataclass(frozen=True)
class ActiveCodeEntry:
    """Ficha consolidada de un código promocional vigente para el resumen semanal."""

    code: str
    game: str
    game_label: str
    rewards_es: str
    start_date: str | None
    end_date: str | None
    window_label: str
    redeem_url: str
    source_name: str


def build_active_code_entries(
    items: list[AlertItem],
    events_cache: dict[str, dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    *,
    now_utc: datetime | None = None,
    enable_remote: bool = True,
) -> list[ActiveCodeEntry]:
    """Recopila códigos vigentes deduplicados con premios y fechas exactas."""
    ref_today = (now_utc or datetime.now(timezone.utc)).astimezone(_COT_TZ).date()
    cache = events_cache or {}
    seen_topics = (state or {}).get("seen_topics") or {}

    # Mapa auxiliar de promocodes declarados dentro de ScrapedDuck events.json
    event_dates_by_code: dict[str, tuple[str | None, str | None]] = {}
    for ev in cache.values():
        codes = ((ev.get("extraData") or {}).get("promocodes") or [])
        s_raw = (ev.get("start") or "")[:10] or None
        e_raw = (ev.get("end") or "")[:10] or None
        for c in codes:
            c_up = str(c).strip().upper()
            if c_up:
                event_dates_by_code[c_up] = (s_raw, e_raw)

    entries: list[ActiveCodeEntry] = []
    seen_codes: set[tuple[str, str]] = set()
    for item in items:
        if item.kind != "code" or not item.promo_code:
            continue
        code = item.promo_code.strip()
        key = (item.game, code.upper())
        if key in seen_codes:
            continue

        ev_start, ev_end = event_dates_by_code.get(code.upper(), (None, None))
        start_dt = parse_date_flex(item.starts_on) or parse_date_flex(ev_start)
        end_dt = parse_date_flex(item.expires_at) or parse_date_flex(ev_end)
        if end_dt and end_dt < ref_today:
            continue

        seen_codes.add(key)
        detected_iso = seen_topics.get(f"{item.game}:code:{code.upper()}")
        raw_reward = item.rewards or item.title
        rewards_es = (
            raw_reward
            if item.language == "es"
            else translate_en_to_es(raw_reward, enable_remote=enable_remote)
        )
        start_iso = start_dt.isoformat() if start_dt else None
        end_iso = end_dt.isoformat() if end_dt else None
        window = format_validity_window(
            start_iso,
            end_iso,
            detected_iso=detected_iso,
            raw_expires=item.expires_at,
        )
        entries.append(
            ActiveCodeEntry(
                code=code,
                game=item.game,
                game_label="Pokémon GO" if item.game == "pogo" else "Pokémon TCG Pocket",
                rewards_es=rewards_es,
                start_date=start_iso,
                end_date=end_iso,
                window_label=window,
                redeem_url=item.redeem_url or item.url,
                source_name=item.source_name,
            )
        )

    entries.sort(key=lambda e: (e.end_date or "9999-99-99", e.game, e.code))
    return entries


def render_weekly_codes_card(
    entries: list[ActiveCodeEntry],
    output_path: Path,
    *,
    week_label: str,
) -> Path:
    """Dibuja la tarjeta visual PNG (1080×720) del consolidado semanal de códigos."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (_CARD_W, _CARD_H), "#100B24")
    draw = ImageDraw.Draw(img)

    title_font = _load_font("bold", 28)
    sub_font = _load_font("regular", 18)
    badge_font = _load_font("bold", 15)
    row_bold = _load_font("bold", 19)
    row_font = _load_font("regular", 17)

    draw.rounded_rectangle((24, 20, _CARD_W - 24, 140), radius=20, fill="#1E163F", outline="#FACC15", width=3)
    badge_l = "CONSOLIDADO LUNES"
    lw = int(draw.textlength(badge_l, font=badge_font)) + 26
    draw.rounded_rectangle((44, 34, 44 + lw, 62), radius=12, fill="#FACC15")
    draw.text((57, 39), badge_l, font=badge_font, fill="#422006")

    badge_r = f"ACTIVOS: {len(entries)}"
    rw = int(draw.textlength(badge_r, font=badge_font)) + 26
    draw.rounded_rectangle((_CARD_W - 44 - rw, 34, _CARD_W - 44, 62), radius=12, fill="#14532D")
    draw.text((_CARD_W - 44 - rw + 13, 39), badge_r, font=badge_font, fill="#DCFCE7")

    draw.text((44, 72), "Códigos Promocionales Vigentes", font=title_font, fill="#F8FAFC")
    draw.text((44, 108), f"Revisión automática • {week_label} • Listos para reclamar", font=sub_font, fill="#FDE047")

    draw.rounded_rectangle((24, 156, _CARD_W - 24, 666), radius=20, fill="#191236", outline="#4C1D95", width=2)
    y = 176
    if not entries:
        draw.text((48, 210), "Sin códigos promocionales activos esta semana.", font=title_font, fill="#CBD5E1")
    else:
        max_show = min(len(entries), 6)
        block_h = 78 if max_show <= 5 else 68
        for idx, ent in enumerate(entries[:max_show], 1):
            tag = "[GO]" if ent.game == "pogo" else "[TCGP]"
            tag_color = "#38BDF8" if ent.game == "pogo" else "#C084FC"
            draw.text((44, y), f"{idx}. {tag}", font=row_bold, fill=tag_color)
            tw = int(draw.textlength(f"{idx}. {tag} ", font=row_bold))
            draw.text((44 + tw, y), ent.code, font=row_bold, fill="#FACC15")
            detail = _wrap_text(
                draw,
                f"Premio: {ent.rewards_es} | {ent.window_label}",
                row_font,
                _CARD_W - 108,
                1,
            )
            if detail:
                draw.text((64, y + 26), detail[0], font=row_font, fill="#E2E8F0")
            y += block_h

    draw.text((28, 682), "Toca cualquier código en el mensaje para copiarlo y usa el botón Canjear", font=sub_font, fill="#94A3B8")
    img.save(output_path, format="PNG", optimize=True)
    return output_path


def build_weekly_codes_caption(entries: list[ActiveCodeEntry], *, week_label: str) -> str:
    """Construye el pie HTML para Telegram con códigos copiables y vigencia."""
    if not entries:
        return (
            f"🎁 <b>Consolidado semanal de códigos ({html.escape(week_label)})</b>\n\n"
            "📭 No hay códigos promocionales activos reportados esta semana en Pokémon GO ni en TCG Pocket."
        )

    lines = [
        f"🎁 <b>Consolidado semanal de códigos activos ({len(entries)})</b>",
        f"📅 <i>Corte semanal: {html.escape(week_label)}</i>",
        "",
    ]
    for idx, ent in enumerate(entries, 1):
        lines.append(
            f"{idx}. <b>{html.escape(ent.game_label)}</b> • <code>{html.escape(ent.code)}</code>\n"
            f"   🎁 {html.escape(ent.rewards_es)}\n"
            f"   ⏳ {html.escape(ent.window_label)}"
        )
    lines.append("")
    lines.append("📋 <i>Toca cada código para copiarlo directo y presiona el botón para canjear.</i>")
    return "\n".join(lines)[:TELEGRAM_CAPTION_LIMIT]


def build_weekly_codes_keyboard(entries: list[ActiveCodeEntry]) -> dict[str, list[list[dict[str, str]]]]:
    """Genera botones directos de canje (hasta 4 códigos en 2 filas)."""
    rows: list[list[dict[str, str]]] = []
    cur: list[dict[str, str]] = []
    for ent in entries[:4]:
        if not ent.redeem_url:
            continue
        cur.append({"text": f"🎁 Canjear {ent.code[:18]}", "url": ent.redeem_url})
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return {"inline_keyboard": rows}
