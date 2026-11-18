"""Cliente amigable para enviar tarjetas PNG, láminas QR de barajas y botones a Telegram."""

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
from src.deck_qr import (
    ENERGY_ID_LABELS_ES,
    BuiltDeck,
    build_qr_download_url,
    describe_deck_strategy,
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
    raw_header = (
        "🚀 ¡YA DISPONIBLE HOY!"
        if item.is_launch_reminder
        else TELEGRAM_KIND_HEADERS.get((item.game, item.kind), "✨ ¡Nueva alerta Pokémon!")
    )
    kind_header = html.escape(raw_header)
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

    while len(caption) > TELEGRAM_CAPTION_LIMIT and len(lines) > 6:
        lines.pop(len(lines) - 2)
        caption = "\n".join(lines)

    return caption[:TELEGRAM_CAPTION_LIMIT]


def build_single_deck_caption(
    deck: BuiltDeck,
    *,
    context_title: str,
    header_tag: str = "🃏 <b>Baraja recomendada</b>",
    extra_note: str | None = None,
) -> str:
    """Construye la explicación individual en HTML para una sola baraja con QR."""
    energy = "/".join(ENERGY_ID_LABELS_ES.get(e, "?") for e in deck.energy_types)
    strategy = describe_deck_strategy(deck)
    lines = [
        f"{header_tag} • <b>{html.escape(deck.title)}</b>",
        f"📌 <b>Contexto:</b> {html.escape(context_title)}",
        f"🎯 <b>Ventaja:</b> {html.escape(deck.mission_tag)} | ⚡ <b>Energía:</b> {html.escape(energy)}",
        "",
        f"💡 <b>Explicación:</b> {html.escape(strategy)}",
    ]
    if extra_note:
        lines.append(f"📊 <i>{html.escape(extra_note)}</i>")
    lines.append("")
    lines.append("📲 <i>Guarda la foto en tu galería o toca «Descargar QR» para importarla directo.</i>")
    return "\n".join(lines)[:TELEGRAM_CAPTION_LIMIT]


def build_single_deck_keyboard(
    deck: BuiltDeck,
    *,
    info_label: str,
    info_url: str,
) -> dict[str, list[list[dict[str, str]]]]:
    """Crea exactamente 2 botones por baraja: descarga del QR y enlace de contexto."""
    buttons = [{"text": "📥 Descargar QR", "url": build_qr_download_url(deck.qr_payload)}]
    if info_url:
        buttons.append({"text": info_label, "url": info_url})
    return {"inline_keyboard": [buttons]}


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


def _post_photo_to_telegram(
    *,
    bot_token: str,
    chat_id: str,
    caption: str,
    image_path: Path,
    keyboard_json: str | None = None,
) -> None:
    """Envía una imagen PNG con reintento automático ante límites 429 de Telegram."""
    api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    data: dict[str, str] = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if keyboard_json:
        data["reply_markup"] = keyboard_json

    for attempt in range(2):
        with image_path.open("rb") as photo_file:
            resp = requests.post(
                api_url,
                data=data,
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


def send_telegram_photo(
    *,
    bot_token: str,
    chat_id: str,
    item: AlertItem,
    image_path: Path,
) -> None:
    """Envía la tarjeta PNG principal del evento o código promocional."""
    _post_photo_to_telegram(
        bot_token=bot_token,
        chat_id=chat_id,
        caption=build_telegram_caption(item),
        image_path=image_path,
        keyboard_json=json.dumps(build_inline_keyboard(item), ensure_ascii=False),
    )


def send_telegram_single_deck(
    *,
    bot_token: str,
    chat_id: str,
    deck: BuiltDeck,
    card_path: Path,
    context_title: str,
    header_tag: str,
    info_label: str,
    info_url: str,
    extra_note: str | None = None,
) -> None:
    """Envía una tarjeta PNG individual con explicación y 2 botones (QR + contexto)."""
    caption = build_single_deck_caption(
        deck,
        context_title=context_title,
        header_tag=header_tag,
        extra_note=extra_note,
    )
    keyboard = build_single_deck_keyboard(deck, info_label=info_label, info_url=info_url)
    _post_photo_to_telegram(
        bot_token=bot_token,
        chat_id=chat_id,
        caption=caption,
        image_path=card_path,
        keyboard_json=json.dumps(keyboard, ensure_ascii=False),
    )
