"""Cliente amigable para enviar tarjetas PNG y códigos copiables a Telegram."""

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


def build_telegram_caption(item: AlertItem) -> str:
    """Construye un mensaje HTML cálido, legible y con código copiable en 1 toque."""
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
    lines.append(f"📡 <i>Fuente: {html.escape(item.source_name)}</i>")

    caption = "\n".join(lines)
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption

    overflow = len(caption) - TELEGRAM_CAPTION_LIMIT + 3
    trimmed_summary = summary[:-overflow].rstrip() + "..." if overflow < len(summary) else ""
    lines[-2] = f"💬 {trimmed_summary}" if trimmed_summary else ""
    return "\n".join(line for line in lines if line is not None)[:TELEGRAM_CAPTION_LIMIT]


def build_inline_keyboard(item: AlertItem) -> dict[str, list[list[dict[str, str]]]]:
    """Crea botones URL directos para canjear el código o abrir el detalle oficial."""
    buttons: list[dict[str, str]] = []
    if item.redeem_url:
        buttons.append({"text": "🎁 Canjear código ahora", "url": item.redeem_url})
    if item.url:
        buttons.append({"text": "🔎 Ver detalles", "url": item.url})
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
