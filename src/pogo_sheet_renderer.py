"""Renderizador de tarjetas tácticas PNG (1080×720) para Pokémon GO en Telegram."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

from src.card_renderer import _load_font, _wrap_text
from src.pogo_intel import TYPE_LABELS_ES, PogoTacticalCard

CARD_WIDTH = 1080
CARD_HEIGHT = 720

_TYPE_COLORS: dict[str, tuple[str, str]] = {
    "fire": ("#EF4444", "#450A0A"),
    "water": ("#38BDF8", "#082F49"),
    "ice": ("#67E8F9", "#083344"),
    "grass": ("#22C55E", "#052E16"),
    "bug": ("#84CC16", "#1A2E05"),
    "electric": ("#FACC15", "#422006"),
    "psychic": ("#C084FC", "#3B0764"),
    "ghost": ("#A855F7", "#2E1065"),
    "poison": ("#E879F9", "#4A044E"),
    "fighting": ("#FB923C", "#431407"),
    "ground": ("#F59E0B", "#451A03"),
    "rock": ("#D6D3D1", "#1C1917"),
    "steel": ("#94A3B8", "#0F172A"),
    "dark": ("#64748B", "#F8FAFC"),
    "dragon": ("#818CF8", "#1E1B4B"),
    "fairy": ("#F472B6", "#500724"),
    "flying": ("#7DD3FC", "#0C4A6E"),
    "normal": ("#CBD5E1", "#1E293B"),
}


def render_pogo_tactical_card(card: PogoTacticalCard, output_path: Path) -> Path:
    """Dibuja una tarjeta táctica GO limpia con datos 100%, counters/PvP y filtro rápido."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), "#100B24")
    draw = ImageDraw.Draw(img)

    title_font = _load_font("bold", 28)
    sub_font = _load_font("regular", 18)
    badge_font = _load_font("bold", 15)
    section_font = _load_font("bold", 16)
    row_font = _load_font("regular", 19)

    accent, pill_fg = _TYPE_COLORS.get(card.primary_type.lower(), ("#38BDF8", "#082F49"))
    type_es = TYPE_LABELS_ES.get(card.primary_type.lower(), card.primary_type.title())

    # Cabecera superior
    draw.rounded_rectangle(
        (24, 20, CARD_WIDTH - 24, 140),
        radius=20,
        fill="#1E163F",
        outline=accent,
        width=3,
    )
    pill_text = f"Tipo {type_es}"
    pw = int(draw.textlength(pill_text, font=badge_font)) + 26
    draw.rounded_rectangle((44, 34, 44 + pw, 62), radius=12, fill=accent)
    draw.text((57, 39), pill_text, font=badge_font, fill=pill_fg)

    rw = int(draw.textlength(card.badge_label, font=badge_font)) + 26
    draw.rounded_rectangle((CARD_WIDTH - 44 - rw, 34, CARD_WIDTH - 44, 62), radius=12, fill="#312E81")
    draw.text((CARD_WIDTH - 44 - rw + 13, 39), card.badge_label, font=badge_font, fill="#E0E7FF")

    title_lines = _wrap_text(draw, card.title, title_font, CARD_WIDTH - 96, 1)
    if title_lines:
        draw.text((44, 72), title_lines[0], font=title_font, fill="#F8FAFC")

    sub_lines = _wrap_text(draw, card.subtitle, sub_font, CARD_WIDTH - 96, 1)
    if sub_lines:
        draw.text((44, 108), sub_lines[0], font=sub_font, fill="#FDE047")

    # Paneles izquierdo (datos/debilidades/PC) y derecho (counters/PvP)
    panels = (
        (24, 512, "#4C1D95", card.left_heading, card.left_rows, "#FACC15", "#F8FAFC", 450),
        (530, CARD_WIDTH - 24, accent, card.right_heading, card.right_rows, "#38BDF8", "#E0F2FE", 488),
    )
    for x0, x1, border_col, heading, rows, hd_col, txt_col, max_w in panels:
        draw.rounded_rectangle(
            (x0, 156, x1, 598),
            radius=20,
            fill="#191236",
            outline=border_col,
            width=2,
        )
        draw.text((x0 + 20, 174), heading, font=section_font, fill=hd_col)
        ry = 212
        step = 34 if len(rows) <= 9 else 28
        for raw_row in rows:
            for line in _wrap_text(draw, raw_row, row_font, max_w, 2):
                if ry > 562:
                    break
                is_sep = line.startswith("—")
                draw.text(
                    (x0 + 20, ry),
                    line,
                    font=badge_font if is_sep else row_font,
                    fill="#FACC15" if is_sep else txt_col,
                )
                ry += step - (4 if is_sep else 0)
            if ry > 562:
                break

    # Franja inferior con cadena de búsqueda lista para copiar en Telegram
    draw.rounded_rectangle(
        (24, 612, CARD_WIDTH - 24, 668),
        radius=14,
        fill="#1E163F",
        outline="#4C1D95",
        width=2,
    )
    tag = "FILTRO GO:"
    draw.text((42, 631), tag, font=section_font, fill="#FACC15")
    tw = int(draw.textlength(tag, font=section_font))
    s_lines = _wrap_text(draw, card.search_string, row_font, CARD_WIDTH - 96 - tw, 1)
    if s_lines:
        draw.text((54 + tw, 629), s_lines[0], font=row_font, fill="#E2E8F0")

    footer = _wrap_text(draw, card.context_label, sub_font, CARD_WIDTH - 56, 1)
    if footer:
        draw.text((28, 682), footer[0], font=sub_font, fill="#94A3B8")

    img.save(output_path, format="PNG", optimize=True)
    return output_path
