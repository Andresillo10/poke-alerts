"""Renderizador de tarjetas PNG individuales (1 QR único por imagen) para Telegram."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import qrcode
from PIL import Image, ImageDraw

from src.card_renderer import _load_font, _wrap_text
from src.deck_qr import ENERGY_ID_LABELS_ES, BuiltDeck

CARD_WIDTH = 1080
CARD_HEIGHT = 720

_ENERGY_COLORS = {
    1: ("#22C55E", "#052E16"),
    2: ("#EF4444", "#450A0A"),
    3: ("#38BDF8", "#082F49"),
    4: ("#FACC15", "#422006"),
    5: ("#C084FC", "#3B0764"),
    6: ("#FB923C", "#431407"),
    7: ("#94A3B8", "#0F172A"),
    8: ("#CBD5E1", "#1E293B"),
}


def _ordered_card_entries(deck: BuiltDeck) -> list[tuple[bool, str]]:
    """Devuelve todas las cartas únicas ordenadas (Pokémon primero) con su cantidad."""
    counts = Counter((c.is_pokemon, c.name) for c in deck.cards)
    ordered = sorted(counts.items(), key=lambda kv: (not kv[0][0], -kv[1], kv[0][1]))
    return [(is_pk, f"{qty}× {name}") for (is_pk, name), qty in ordered]


def render_single_deck_card(
    deck: BuiltDeck,
    output_path: Path,
    *,
    context_label: str,
    badge_label: str = "EVENTO TCG POCKET",
) -> Path:
    """Dibuja una tarjeta PNG individual con título, lista completa y UN solo QR."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), "#100B24")
    draw = ImageDraw.Draw(img)

    title_font = _load_font("bold", 28)
    sub_font = _load_font("regular", 18)
    badge_font = _load_font("bold", 15)
    section_font = _load_font("bold", 16)
    card_font = _load_font("regular", 19)

    primary_energy = deck.energy_types[0] if deck.energy_types else 5
    accent, badge_fg = _ENERGY_COLORS.get(primary_energy, ("#A78BFA", "#2E1065"))
    energy_label = "/".join(ENERGY_ID_LABELS_ES.get(e, "?") for e in deck.energy_types)

    # Cabecera superior con energía, insignia, nombre del mazo y misión/stats
    draw.rounded_rectangle(
        (24, 20, CARD_WIDTH - 24, 140),
        radius=20,
        fill="#1E163F",
        outline=accent,
        width=3,
    )
    pill_text = f"Energía {energy_label}"
    pw = int(draw.textlength(pill_text, font=badge_font)) + 26
    draw.rounded_rectangle((44, 34, 44 + pw, 62), radius=12, fill=accent)
    draw.text((57, 39), pill_text, font=badge_font, fill=badge_fg)

    rw = int(draw.textlength(badge_label, font=badge_font)) + 26
    draw.rounded_rectangle((CARD_WIDTH - 44 - rw, 34, CARD_WIDTH - 44, 62), radius=12, fill="#312E81")
    draw.text((CARD_WIDTH - 44 - rw + 13, 39), badge_label, font=badge_font, fill="#E0E7FF")

    title_lines = _wrap_text(draw, deck.title, title_font, CARD_WIDTH - 96, 1)
    if title_lines:
        draw.text((44, 72), title_lines[0], font=title_font, fill="#F8FAFC")

    tag_lines = _wrap_text(draw, deck.mission_tag, sub_font, CARD_WIDTH - 96, 1)
    if tag_lines:
        draw.text((44, 108), tag_lines[0], font=sub_font, fill="#FDE047")

    # Columna izquierda: listado completo separado por Pokémon y Entrenadores
    draw.rounded_rectangle(
        (24, 156, 610, 668),
        radius=20,
        fill="#191236",
        outline="#4C1D95",
        width=2,
    )
    entries = _ordered_card_entries(deck)
    pk_entries = [line for is_pk, line in entries if is_pk]
    tr_entries = [line for is_pk, line in entries if not is_pk]
    pk_total = sum(1 for c in deck.cards if c.is_pokemon)
    tr_total = sum(1 for c in deck.cards if not c.is_pokemon)

    step = 24 if len(entries) <= 15 else 20
    ly = 172

    def _draw_section(heading: str, rows: list[str], heading_color: str, row_color: str) -> None:
        nonlocal ly
        if not rows or ly > 635:
            return
        draw.text((44, ly), heading, font=section_font, fill=heading_color)
        ly += 24
        for line in rows:
            if ly > 646:
                break
            trimmed = _wrap_text(draw, line, card_font, 536, 1)
            if trimmed:
                draw.text((44, ly), trimmed[0], font=card_font, fill=row_color)
                ly += step
        ly += 8

    _draw_section(f"POKÉMON ({pk_total})", pk_entries, "#FACC15", "#F8FAFC")
    _draw_section(f"ENTRENADORES ({tr_total})", tr_entries, "#38BDF8", "#BAE6FD")

    # Columna derecha: un único QR grande y escaneable desde galería
    draw.rounded_rectangle(
        (630, 156, CARD_WIDTH - 24, 668),
        radius=20,
        fill="#191236",
        outline=accent,
        width=2,
    )
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(deck.qr_payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 336
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)

    panel_center_x = (630 + (CARD_WIDTH - 24)) // 2
    qr_x = panel_center_x - (qr_size // 2)
    qr_y = 192
    draw.rounded_rectangle(
        (qr_x - 14, qr_y - 14, qr_x + qr_size + 14, qr_y + qr_size + 14),
        radius=16,
        fill="#FFFFFF",
    )
    img.paste(qr_img, (qr_x, qr_y))

    note_1 = "1 SOLO QR • LISTO PARA GALERÍA"
    w1 = draw.textlength(note_1, font=section_font)
    draw.text((panel_center_x - w1 / 2, 566), note_1, font=section_font, fill="#FACC15")

    note_2 = "Barajas → + Crear nueva → Leer QR"
    w2 = draw.textlength(note_2, font=sub_font)
    draw.text((panel_center_x - w2 / 2, 598), note_2, font=sub_font, fill="#CBD5E1")

    footer = _wrap_text(draw, context_label, sub_font, CARD_WIDTH - 56, 1)
    if footer:
        draw.text((28, 682), footer[0], font=sub_font, fill="#94A3B8")

    img.save(output_path, format="PNG", optimize=True)
    return output_path
