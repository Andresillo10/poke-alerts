"""Cliente amigable para enviar tarjetas PNG, guías y botones a Telegram."""

from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path

import requests

from src.config import (
    REQUEST_TIMEOUT_SEC,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_GAME_NAMES,
    TELEGRAM_KIND_HEADERS,
)
from src.models import AlertItem

LOGGER = logging.getLogger(__name__)


def _format_bullet(bullet: str) -> str:
    """Agrega ícono visual según el tipo de detalle del evento."""
    lower = bullet.lower()
    if lower.startswith(("pokémon salvajes", "estado salvaje", "wild spawns", "pokémon destacados", "featured pokémon")):
        return f"🌿 {html.escape(bullet)}"
    if lower.startswith(("misiones", "missions")):
        return f"🎯 {html.escape(bullet)}"
    if lower.startswith(("bonus", "bonificaciones")):
        return f"🔥 {html.escape(bullet)}"
    if lower.startswith(("cartas", "featured cards")):
        return f"🃏 {html.escape(bullet)}"
    return f"• {html.escape(bullet)}"


def build_telegram_caption(item: AlertItem) -> str:
    """Construye un mensaje HTML cálido con misiones/spawns/mazos dentro de 1024 chars."""
    game_name = html.escape(TELEGRAM_GAME_NAMES[item.game])
    kind_header = html.escape(
        TELEGRAM_KIND_HEADERS.get((item.game, item.kind), "✨ ¡Nueva alerta Pokémon!")
    )
    title = html.escape(item.title)
    summary = html.escape(item.summary)

    lines = [
        f"<b>{game_name}</b> • {kind_header}",
        f"✨ <b>{title}</b>",
    ]
    if item.rewards:
        lines.append(f"🎁 <b>Recompensa:</b> {html.escape(item.rewards)}")
    if item.expires_at:
        date_label = "Válido hasta" if item.kind == "code" else "Fechas"
        lines.append(f"⏳ <b>{date_label}:</b> {html.escape(item.expires_at)}")

    if item.promo_code:
        lines.append("")
        lines.append("📋 <b>Toca el código para copiarlo:</b>")
        lines.append(f"<code>{html.escape(item.promo_code)}</code>")

    lines.append("")
    lines.append(f"💬 {summary}")

    if item.guide_bullets:
        lines.append("")
        for bullet in item.guide_bullets[:4]:
            lines.append(_format_bullet(bullet))

    if item.deck_guide_title:
        lines.append(f"📘 <b>Guía / Mazos:</b> {html.escape(item.deck_guide_title)}")

    lines.append(f"📡 <i>Fuente: {html.escape(item.source_name)}</i>")

    caption = "\n".join(lines)
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption

    # Recorta progresivamente las viñetas menos prioritarias hasta ajustar a 1024 chars.
    while len(caption) > TELEGRAM_CAPTION_LIMIT and len(lines) > 6:
        drop_idx = len(lines) - 2
        lines.pop(drop_idx)
        caption = "\n".join(lines)

    return caption[:TELEGRAM_CAPTION_LIMIT]


def build_inline_keyboard(item: AlertItem) -> dict[str, list[list[dict[str, str]]]]:
    """Crea botones URL directos para canje, detalle oficial y guías de mazos."""
    buttons: list[dict[str, str]] = []
    if item.redeem_url:
        buttons.append({"text": "🎁 Canjear código", "url": item.redeem_url})
    if item.url:
        buttons.append({"text": "🔎 Ver detalles", "url": item.url})
    if item.deck_guide_url:
        buttons.append({"text": "🃏 Mazos recomendados", "url": item.deck_guide_url})
    return {"inline_keyboard": [buttons]} if buttons else {"inline_keyboard": []}


def send_telegram_photo(
    *,
    bot_token: str,
    chat_id: str,
    item: AlertItem,
    image_path: Path,
) -> None:
    """Envía la tarjeta PNG con caption HTML y maneja límites 429 de Telegram."""
    api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    caption = build_telegram_caption(item)
    keyboard = json.dumps(build_inline_keyboard(item), ensure_ascii=False)

    for attempt in range(2):
        with image_path.open("rb") as photo_file:
            resp = requests.post(
                api_url,
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                files={"photo": (image_path.name, photo_file, "image/png")},
                timeout=REQUEST_TIMEOUT_SEC,
            )

        if resp.status_code == 429 and attempt == 0:
            retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
            LOGGER.warning("Rate limit Telegram (429); esperando %ss.", retry_after)
            time.sleep(retry_after + 0.5)
            continue

        if not resp.ok:
            raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:300]}")
        return
