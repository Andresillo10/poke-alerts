"""Generador determinista de tarjetas gráficas PNG temáticas Pokémon con Pillow."""

from __future__ import annotations

import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from src.config import GAME_LABELS, KIND_LABELS
from src.models import AlertItem

CARD_WIDTH = 1080
CARD_HEIGHT = 600
PADDING = 52

_THEMES: dict[tuple[str, str], dict[str, str]] = {
    ("pogo", "code"): {
        "bg": "#0B1324",
        "panel": "#131F38",
        "panel_inner": "#0C1527",
        "watermark": "#1B2C4D",
        "accent": "#22C55E",
        "highlight": "#FACC15",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
        "badge_text": "#052E16",
    },
    ("pogo", "event"): {
        "bg": "#0B1324",
        "panel": "#131F38",
        "panel_inner": "#0C1527",
        "watermark": "#1B2C4D",
        "accent": "#38BDF8",
        "highlight": "#FACC15",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
        "badge_text": "#082F49",
    },
    ("pogo", "news"): {
        "bg": "#0B1324",
        "panel": "#131F38",
        "panel_inner": "#0C1527",
        "watermark": "#1B2C4D",
        "accent": "#818CF8",
        "highlight": "#FDE047",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
        "badge_text": "#1E1B4B",
    },
    ("tcgp", "code"): {
        "bg": "#150E2E",
        "panel": "#201744",
        "panel_inner": "#160F31",
        "watermark": "#2D215C",
        "accent": "#FBBF24",
        "highlight": "#FDE047",
        "text": "#FAF5FF",
        "muted": "#DDD6FE",
        "badge_text": "#451A03",
    },
    ("tcgp", "event"): {
        "bg": "#150E2E",
        "panel": "#201744",
        "panel_inner": "#160F31",
        "watermark": "#2D215C",
        "accent": "#A78BFA",
        "highlight": "#FBBF24",
        "text": "#FAF5FF",
        "muted": "#DDD6FE",
        "badge_text": "#2E1065",
    },
}

_FONT_CANDIDATES = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "segoeuib.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "segoeui.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
    ],
    "mono": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "consolab.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
    ],
}


def _load_font(weight: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Carga una fuente TTF multiplataforma (Linux/Windows) con fallback seguro."""
    for path in _FONT_CANDIDATES.get(weight, []):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Ajusta texto al ancho máximo en píxeles truncando con puntos suspensivos."""
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break

    if len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines and " ".join(lines).split() != words:
        last = lines[-1]
        while last and draw.textlength(last + "...", font=font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = (last + "...") if last else "..."
    return lines


def _fit_single_line_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    weight: str,
    max_width: int,
    start_size: int = 38,
    min_size: int = 20,
) -> ImageFont.ImageFont:
    """Reduce progresivamente la fuente para que un código largo nunca se desborde."""
    for size in range(start_size, min_size - 1, -2):
        font = _load_font(weight, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
    return _load_font(weight, min_size)


def _draw_pokeball_watermark(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    radius: int,
    ring_color: str,
    panel_color: str,
) -> None:
    """Dibuja una Poké Ball sutil de fondo como sello visual sin restar legibilidad."""
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=ring_color,
        width=12,
    )
    draw.rectangle(
        (cx - radius + 4, cy - 8, cx + radius - 4, cy + 8),
        fill=ring_color,
    )
    inner_r = int(radius * 0.30)
    draw.ellipse(
        (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
        fill=panel_color,
        outline=ring_color,
        width=10,
    )
    dot_r = int(radius * 0.13)
    draw.ellipse(
        (cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r),
        fill=ring_color,
    )


def _draw_header_emblem(draw: ImageDraw.ImageDraw, game: str, x: int, y: int, accent: str) -> None:
    """Dibuja un ícono nítido de Poké Ball (GO) o Carta Dorada (TCG Pocket)."""
    if game == "pogo":
        r = 22
        cx, cy = x + r, y + r
        draw.pieslice((cx - r, cy - r, cx + r, cy + r), 180, 360, fill="#EF4444")
        draw.pieslice((cx - r, cy - r, cx + r, cy + r), 0, 180, fill="#F8FAFC")
        draw.rectangle((cx - r, cy - 3, cx + r, cy + 3), fill="#0F172A")
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline="#0F172A", width=3)
        draw.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill="#F8FAFC", outline="#0F172A", width=3)
        return

    # Carta estilizada TCG Pocket
    draw.rounded_rectangle((x + 4, y + 2, x + 34, y + 44), radius=5, fill=accent, outline="#FDE047", width=2)
    draw.rounded_rectangle((x + 9, y + 8, x + 29, y + 25), radius=3, fill="#1E1B4B")
    draw.line((x + 10, y + 32, x + 28, y + 32), fill="#FEF08A", width=3)


def render_alert_card(item: AlertItem, output_path: Path) -> Path:
    """Renderiza una tarjeta PNG temática Pokémon (<100 KB) y la guarda en disco."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    theme = _THEMES.get((item.game, item.kind), _THEMES[("tcgp", "event")])

    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)

    # Marco principal de la tarjeta
    draw.rounded_rectangle(
        (20, 20, CARD_WIDTH - 20, CARD_HEIGHT - 20),
        radius=28,
        fill=theme["panel"],
        outline=theme["accent"],
        width=3,
    )
    _draw_pokeball_watermark(
        draw,
        cx=CARD_WIDTH - 145,
        cy=235,
        radius=175,
        ring_color=theme["watermark"],
        panel_color=theme["panel"],
    )

    # Barra superior de acento
    draw.rounded_rectangle((20, 20, CARD_WIDTH - 20, 36), radius=10, fill=theme["accent"])

    badge_font = _load_font("bold", 20)
    title_font = _load_font("bold", 38)
    body_font = _load_font("regular", 24)
    label_font = _load_font("bold", 20)
    footer_font = _load_font("regular", 20)

    _draw_header_emblem(draw, item.game, PADDING, 58, theme["highlight"])
    badge_text = f"{GAME_LABELS[item.game]}  •  {KIND_LABELS[item.kind]}"
    badge_x = PADDING + 56
    badge_w = int(draw.textlength(badge_text, font=badge_font)) + 34
    draw.rounded_rectangle((badge_x, 60, badge_x + badge_w, 102), radius=18, fill=theme["accent"])
    draw.text((badge_x + 17, 70), badge_text, font=badge_font, fill=theme["badge_text"])

    content_w = CARD_WIDTH - (PADDING * 2)
    y = 128
    for line in _wrap_text(draw, item.title, title_font, content_w, max_lines=2):
        draw.text((PADDING, y), line, font=title_font, fill=theme["text"])
        y += 48

    y += 12
    if item.rewards:
        reward_line = f"Recompensa: {item.rewards}"
        for line in _wrap_text(draw, reward_line, label_font, content_w, max_lines=1):
            draw.text((PADDING, y), line, font=label_font, fill=theme["highlight"])
            y += 32

    if item.promo_code:
        max_body_lines = 3 if item.rewards else 4
        for line in _wrap_text(draw, item.summary, body_font, content_w, max_lines=max_body_lines):
            draw.text((PADDING, y), line, font=body_font, fill=theme["muted"])
            y += 32

        box_top = 374
        box_bottom = 482
        draw.rounded_rectangle(
            (PADDING, box_top, CARD_WIDTH - PADDING, box_bottom),
            radius=18,
            fill=theme["panel_inner"],
            outline=theme["highlight"],
            width=3,
        )
        draw.text(
            (PADDING + 24, box_top + 14),
            "CÓDIGO DE REGALO (TOCA EL CÓDIGO EN EL MENSAJE PARA COPIAR):",
            font=label_font,
            fill=theme["highlight"],
        )
        code_font = _fit_single_line_font(draw, item.promo_code, "mono", content_w - 48)
        draw.text((PADDING + 24, box_top + 46), item.promo_code, font=code_font, fill="#FFFFFF")
    else:
        # Tarjeta de evento/noticia con caja de detalle equilibrada
        box_top = max(y + 10, 260)
        box_bottom = 482
        draw.rounded_rectangle(
            (PADDING, box_top, CARD_WIDTH - PADDING, box_bottom),
            radius=18,
            fill=theme["panel_inner"],
            outline=theme["accent"],
            width=2,
        )
        draw.text(
            (PADDING + 24, box_top + 16),
            "RESUMEN Y CÓMO APROVECHARLO:",
            font=label_font,
            fill=theme["highlight"],
        )
        inner_y = box_top + 50
        max_summary_lines = 2 if item.guide_bullets else 3
        for line in _wrap_text(draw, item.summary, body_font, content_w - 48, max_lines=max_summary_lines):
            draw.text((PADDING + 24, inner_y), line, font=body_font, fill=theme["text"])
            inner_y += 32

        if item.guide_bullets:
            inner_y += 4
            for bullet in item.guide_bullets[:2]:
                for line in _wrap_text(draw, f"• {bullet}", footer_font, content_w - 48, max_lines=1):
                    if inner_y <= box_bottom - 26:
                        draw.text((PADDING + 24, inner_y), line, font=footer_font, fill=theme["muted"])
                        inner_y += 28
        else:
            tip = (
                "Consejo: toca 'Ver detalles' abajo para revisar misiones y horarios."
                if item.game == "tcgp"
                else "Consejo: toca 'Ver detalles' abajo para revisar Pokémon y bonus."
            )
            draw.text((PADDING + 24, box_bottom - 38), tip, font=footer_font, fill=theme["muted"])

    # Pie informativo
    draw.line((PADDING, 512, CARD_WIDTH - PADDING, 512), fill="#334155", width=2)
    date_prefix = "Válido hasta:" if item.kind == "code" else "Fechas:"
    left_footer = f"{date_prefix} {item.expires_at}" if item.expires_at else "Estado: Activo ahora"
    right_footer = f"Fuente: {item.source_name}"

    if draw.textlength(left_footer, font=footer_font) > content_w - 260:
        left_footer = _wrap_text(draw, left_footer, footer_font, content_w - 260, 1)[0]

    draw.text((PADDING, 530), left_footer, font=footer_font, fill=theme["muted"])
    right_w = draw.textlength(right_footer, font=footer_font)
    draw.text((CARD_WIDTH - PADDING - right_w, 530), right_footer, font=footer_font, fill=theme["muted"])

    img.save(output_path, format="PNG", optimize=True)
    return output_path
