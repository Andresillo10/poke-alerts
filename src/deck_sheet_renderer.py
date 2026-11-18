"""Renderizador de lámina PNG 3-en-1 con los códigos QR de barajas para Telegram."""

from __future__ import annotations

from pathlib import Path
import qrcode
from PIL import Image, ImageDraw

from src.card_renderer import _load_font, _wrap_text
from src.deck_qr import ENERGY_ID_LABELS_ES, BuiltDeck

SHEET_WIDTH = 1440
SHEET_HEIGHT = 760

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


def render_deck_sheet(
    event_title: str,
    decks: list[BuiltDeck],
    output_path: Path,
    *,
    header_text: str = "3 MEJORES BARAJAS RECOMENDADAS  •  ESCANEA EL QR EN TCG POCKET",
    subtitle_prefix: str = "Evento",
) -> Path:
    """Dibuja una lámina PNG con hasta 3 barajas y sus QR escaneables lado a lado."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (SHEET_WIDTH, SHEET_HEIGHT), "#100B24")
    draw = ImageDraw.Draw(img)

    title_font = _load_font("bold", 30)
    sub_font = _load_font("regular", 20)
    col_title_font = _load_font("bold", 22)
    badge_font = _load_font("bold", 16)
    list_font = _load_font("regular", 17)

    # Cabecera
    draw.rounded_rectangle((20, 18, SHEET_WIDTH - 20, 102), radius=18, fill="#1E163F", outline="#8B5CF6", width=2)
    title_lines = _wrap_text(draw, header_text, title_font, SHEET_WIDTH - 88, 1)
    if title_lines:
        draw.text((44, 28), title_lines[0], font=title_font, fill="#FACC15")
    event_line = _wrap_text(draw, f"{subtitle_prefix}: {event_title}", sub_font, SHEET_WIDTH - 88, 1)
    if event_line:
        draw.text((44, 68), event_line[0], font=sub_font, fill="#E2E8F0")

    cols = max(len(decks), 1)
    gap = 22
    col_w = (SHEET_WIDTH - 40 - gap * (cols - 1)) // cols
    top_y = 118
    bottom_y = SHEET_HEIGHT - 54

    for idx, deck in enumerate(decks):
        x0 = 20 + idx * (col_w + gap)
        x1 = x0 + col_w
        primary_energy = deck.energy_types[0] if deck.energy_types else 5
        accent, badge_fg = _ENERGY_COLORS.get(primary_energy, ("#A78BFA", "#2E1065"))

        draw.rounded_rectangle(
            (x0, top_y, x1, bottom_y),
            radius=20,
            fill="#191236",
            outline=accent,
            width=3,
        )

        # Etiqueta de energía + misión
        energy_label = "/".join(ENERGY_ID_LABELS_ES.get(e, "?") for e in deck.energy_types)
        pill_text = f"Energía {energy_label}"
        pw = int(draw.textlength(pill_text, font=badge_font)) + 24
        draw.rounded_rectangle((x0 + 18, top_y + 16, x0 + 18 + pw, top_y + 44), radius=12, fill=accent)
        draw.text((x0 + 30, top_y + 21), pill_text, font=badge_font, fill=badge_fg)

        ty = top_y + 54
        for line in _wrap_text(draw, deck.title, col_title_font, col_w - 36, 1):
            draw.text((x0 + 18, ty), line, font=col_title_font, fill="#F8FAFC")
            ty += 28
        for line in _wrap_text(draw, deck.mission_tag, badge_font, col_w - 36, 1):
            draw.text((x0 + 18, ty), line, font=badge_font, fill="#FDE047")
            ty += 24

        # QR escaneable con margen blanco limpio
        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(deck.qr_payload)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = 216
        qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)
        qr_x = x0 + (col_w - qr_size) // 2
        qr_y = ty + 8
        draw.rounded_rectangle(
            (qr_x - 8, qr_y - 8, qr_x + qr_size + 8, qr_y + qr_size + 8),
            radius=12,
            fill="#FFFFFF",
        )
        img.paste(qr_img, (qr_x, qr_y))

        # Lista compacta de cartas debajo del QR
        ly = qr_y + qr_size + 18
        for card_line in deck.summary_lines()[:11]:
            if ly > bottom_y - 22:
                break
            trimmed = _wrap_text(draw, card_line, list_font, col_w - 36, 1)
            if trimmed:
                draw.text((x0 + 18, ly), trimmed[0], font=list_font, fill="#CBD5E1")
                ly += 21

    draw.text(
        (24, SHEET_HEIGHT - 36),
        "En Pokémon TCG Pocket: Barajas → + Crear nueva → Leer código QR",
        font=sub_font,
        fill="#94A3B8",
    )
    img.save(output_path, format="PNG", optimize=True)
    return output_path
