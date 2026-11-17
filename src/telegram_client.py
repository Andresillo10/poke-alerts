"""Cliente ligero para enviar tarjetas PNG y códigos copiables a Telegram."""

from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path

import requests

from src.config import GAME_LABELS, KIND_LABELS, REQUEST_TIMEOUT_SEC, TELEGRAM_CAPTION_LIMIT
from src.models import AlertItem

LOGGER = logging.getLogger(__name__)


def build_telegram_caption(item: AlertItem) -> str:
    """Construye el texto HTML de Telegram con código copiable en 1 toque y límite seguro."""
    game_label = html.escape(GAME_LABELS[item.game].title())
    kind_label = html.escape(KIND_LABELS[item.kind].capitalize())
    title = html.escape(item.title)
    summary = html.escape(item.summary)

    lines = [
        f"<b>[{game_label}] {kind_label}</b>",
        f"<b>{title}</b>",
    ]
    if item.promo_code:
        lines.append("")
        lines.append("📋 <b>Código (toca para copiar):</b>")
        lines.append(f"<code>{html.escape(item.promo_code)}</code>")
    if item.rewards:
        lines.append(f"🎁 <b>Recompensa:</b> {html.escape(item.rewards)}")
    if item.expires_at:
        lines.append(f"⏳ <b>Vigencia / Fecha:</b> {html.escape(item.expires_at)}")

    lines.append("")
    lines.append(summary)
    lines.append(f"<i>Fuente: {html.escape(item.source_name)}</i>")

    caption = "\n".join(lines)
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption

    # Recorta el resumen manteniendo intacto el código copiable y cabecera.
    overflow = len(caption) - TELEGRAM_CAPTION_LIMIT + 3
    trimmed_summary = summary[:-overflow].rstrip() + "..." if overflow < len(summary) else ""
    lines[-2] = trimmed_summary
    return "\n".join(lines)[:TELEGRAM_CAPTION_LIMIT]


def build_inline_keyboard(item: AlertItem) -> dict[str, list[list[dict[str, str]]]]:
    """Crea botones URL directos para canjear el código o abrir el detalle oficial."""
    buttons: list[dict[str, str]] = []
    if item.redeem_url:
        buttons.append({"text": "🎁 Canjear código", "url": item.redeem_url})
    if item.url:
        buttons.append({"text": "🔎 Ver detalle", "url": item.url})
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
