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
from src.code_digest import (
    ActiveCodeEntry,
    build_weekly_codes_caption,
    build_weekly_codes_keyboard,
)
from src.deck_qr import (
    ENERGY_ID_LABELS_ES,
    BuiltDeck,
    build_qr_download_url,
    describe_deck_strategy,
)
from src.models import AlertItem
from src.pogo_intel import PogoTacticalCard

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
    if lower.startswith(("variocolor", "shinies")):
        return f"✨ {html.escape(bullet)}"
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
        lines.append("📋 <b>Toca para copiar:</b>")
        lines.append(f"<code>{html.escape(item.promo_code)}</code>")

    if item.guide_bullets:
        lines.append("")
        for bullet in item.guide_bullets[:4]:
            lines.append(_format_bullet(bullet))
    elif not (item.kind == "code" and item.rewards):
        # Solo muestra resumen breve cuando no hay viñetas ni premio explícito
        short_sum = summary if len(summary) <= 140 else summary[:137].rsplit(" ", 1)[0] + "..."
        if short_sum:
            lines.append("")
            lines.append(f"💬 {short_sum}")

    if item.deck_guide_title:
        lines.append(f"📘 <b>Guía / Mazos:</b> {html.escape(item.deck_guide_title)}")

    lines.append(f"📡 <i>{html.escape(item.source_name)}</i>")

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
        f"⚡ <b>{html.escape(energy)}</b> • {html.escape(deck.mission_tag)}",
        f"💡 <b>Explicación:</b> {html.escape(strategy)}",
    ]
    if extra_note:
        lines.append(f"📊 <i>{html.escape(extra_note)}</i>")
    lines.append("📲 <i>Toca «Descargar QR» para importarla directo en Barajas.</i>")
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


def _post_telegram_api(
    *,
    bot_token: str,
    method: str,
    data: dict[str, str],
    image_path: Path | None = None,
) -> None:
    """Ejecuta una llamada Telegram Bot API con reintento automático ante 429."""
    api_url = f"https://api.telegram.org/bot{bot_token}/{method}"
    for attempt in range(2):
        if image_path is not None:
            with image_path.open("rb") as photo_file:
                resp = requests.post(
                    api_url,
                    data=data,
                    files={"photo": (image_path.name, photo_file, "image/png")},
                    timeout=REQUEST_TIMEOUT_SEC,
                )
        else:
            resp = requests.post(api_url, data=data, timeout=REQUEST_TIMEOUT_SEC)

        if resp.status_code == 429 and attempt == 0:
            retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
            LOGGER.warning("Rate limit Telegram (429); esperando %ss.", retry_after)
            time.sleep(retry_after + 0.5)
            continue

        if not resp.ok:
            raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:300]}")
        return


def _post_photo_to_telegram(
    *,
    bot_token: str,
    chat_id: str,
    caption: str,
    image_path: Path,
    keyboard_json: str | None = None,
) -> None:
    """Envía una imagen PNG con reintento automático ante límites 429 de Telegram."""
    data: dict[str, str] = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if keyboard_json:
        data["reply_markup"] = keyboard_json
    _post_telegram_api(
        bot_token=bot_token,
        method="sendPhoto",
        data=data,
        image_path=image_path,
    )


def send_telegram_text(*, bot_token: str, chat_id: str, text: str) -> None:
    """Envía un mensaje HTML corto de estado a Telegram."""
    _post_telegram_api(
        bot_token=bot_token,
        method="sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )


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


def send_telegram_weekly_codes(
    *,
    bot_token: str,
    chat_id: str,
    entries: list[ActiveCodeEntry],
    card_path: Path,
    week_label: str,
) -> None:
    """Envía la tarjeta consolidada de códigos semanales de los lunes a las 02:00 AM."""
    caption = build_weekly_codes_caption(entries, week_label=week_label)
    keyboard = build_weekly_codes_keyboard(entries)
    _post_photo_to_telegram(
        bot_token=bot_token,
        chat_id=chat_id,
        caption=caption,
        image_path=card_path,
        keyboard_json=json.dumps(keyboard, ensure_ascii=False) if keyboard.get("inline_keyboard") else None,
    )
    if len(entries) > 4:
        extra_lines = [
            "📋 <b>Listado completo de códigos activos esta semana:</b>",
            *[
                f"• <b>{html.escape(e.game_label)}</b>: <code>{html.escape(e.code)}</code> — {html.escape(e.rewards_es)} ({html.escape(e.window_label)})"
                for e in entries[4:]
            ],
        ]
        send_telegram_text(bot_token=bot_token, chat_id=chat_id, text="\n".join(extra_lines)[:4000])


def build_pogo_tactical_caption(
    card: PogoTacticalCard,
    *,
    header_tag: str = "⚡ <b>Guía táctica Pokémon GO</b>",
) -> str:
    """Construye el pie de foto de una tarjeta GO con filtro de búsqueda copiable."""
    lines = [
        f"{header_tag} • <b>{html.escape(card.title)}</b>",
        f"🎯 {html.escape(card.subtitle)}",
        f"💡 {html.escape(card.explanation)}",
        f"📋 <b>Filtro GO (toca para copiar):</b> <code>{html.escape(card.search_string)}</code>",
    ]
    return "\n".join(lines)[:TELEGRAM_CAPTION_LIMIT]


def build_pogo_tactical_keyboard(
    card: PogoTacticalCard,
    *,
    fallback_url: str = "",
) -> dict[str, list[list[dict[str, str]]]]:
    """Construye botón contextual URL para tarjetas tácticas de Pokémon GO."""
    target = card.info_url or fallback_url
    if not target:
        return {"inline_keyboard": []}
    return {"inline_keyboard": [[{"text": card.info_label, "url": target}]]}


def send_telegram_pogo_card(
    *,
    bot_token: str,
    chat_id: str,
    card: PogoTacticalCard,
    card_path: Path,
    header_tag: str = "⚡ <b>Guía táctica Pokémon GO</b>",
    fallback_url: str = "",
) -> None:
    """Envía una tarjeta táctica PNG (1080×720) de Pokémon GO a Telegram."""
    caption = build_pogo_tactical_caption(card, header_tag=header_tag)
    keyboard = build_pogo_tactical_keyboard(card, fallback_url=fallback_url)
    _post_photo_to_telegram(
        bot_token=bot_token,
        chat_id=chat_id,
        caption=caption,
        image_path=card_path,
        keyboard_json=json.dumps(keyboard, ensure_ascii=False),
    )


def send_telegram_weekly_codes(
    *,
    bot_token: str,
    chat_id: str,
    entries: list[ActiveCodeEntry],
    card_path: Path,
    week_label: str,
) -> None:
    """Envía la tarjeta consolidada de códigos semanales de los lunes a las 02:00 AM."""
    caption = build_weekly_codes_caption(entries, week_label=week_label)
    keyboard = build_weekly_codes_keyboard(entries)
    _post_photo_to_telegram(
        bot_token=bot_token,
        chat_id=chat_id,
        caption=caption,
        image_path=card_path,
        keyboard_json=json.dumps(keyboard, ensure_ascii=False) if keyboard.get("inline_keyboard") else None,
    )
    if len(entries) > 4:
        extra_lines = [
            "📋 <b>Listado completo de códigos activos esta semana:</b>",
            *[
                f"• <b>{html.escape(e.game_label)}</b>: <code>{html.escape(e.code)}</code> — {html.escape(e.rewards_es)} ({html.escape(e.window_label)})"
                for e in entries[4:]
            ],
        ]
        send_telegram_text(bot_token=bot_token, chat_id=chat_id, text="\n".join(extra_lines)[:4000])
