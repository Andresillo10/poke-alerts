"""Generador determinista de tarjetas gráficas PNG en español con Pillow."""

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
        "bg": "#0F172A",
        "panel": "#1E293B",
        "accent": "#10B981",
        "highlight": "#F59E0B",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
    },
    ("pogo", "event"): {
        "bg": "#0F172A",
        "panel": "#1E293B",
        "accent": "#0EA5E9",
        "highlight": "#38BDF8",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
    },
    ("pogo", "news"): {
        "bg": "#0F172A",
        "panel": "#1E293B",
        "accent": "#6366F1",
        "highlight": "#A5B4FC",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
    },
    ("tcgp", "code"): {
        "bg": "#140F2D",
        "panel": "#211A44",
        "accent": "#F59E0B",
        "highlight": "#FDE047",
        "text": "#FAF5FF",
        "muted": "#DDD6FE",
    },
    ("tcgp", "event"): {
        "bg": "#140F2D",
        "panel": "#211A44",
        "accent": "#8B5CF6",
        "highlight": "#FBBF24",
        "text": "#FAF5FF",
        "muted": "#DDD6FE",
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


def render_alert_card(item: AlertItem, output_path: Path) -> Path:
    """Renderiza una tarjeta PNG informativa ligera (<100 KB) y la guarda en disco."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    theme = _THEMES.get((item.game, item.kind), _THEMES[("tcgp", "event")])

    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)

    # Tarjeta interior con barra superior de acento
    draw.rounded_rectangle(
        (24, 24, CARD_WIDTH - 24, CARD_HEIGHT - 24),
        radius=26,
        fill=theme["panel"],
        outline=theme["accent"],
        width=3,
    )
    draw.rounded_rectangle(
        (24, 24, CARD_WIDTH - 24, 42),
        radius=12,
        fill=theme["accent"],
    )

    badge_font = _load_font("bold", 21)
    title_font = _load_font("bold", 38)
    body_font = _load_font("regular", 25)
    label_font = _load_font("bold", 20)
    footer_font = _load_font("regular", 20)

    badge_text = f"{GAME_LABELS[item.game]}  •  {KIND_LABELS[item.kind]}"
    badge_w = int(draw.textlength(badge_text, font=badge_font)) + 36
    draw.rounded_rectangle(
        (PADDING, 66, PADDING + badge_w, 108),
        radius=18,
        fill=theme["accent"],
    )
    draw.text((PADDING + 18, 75), badge_text, font=badge_font, fill="#0B1120")

    content_w = CARD_WIDTH - (PADDING * 2)
    y = 132
    for line in _wrap_text(draw, item.title, title_font, content_w, max_lines=2):
        draw.text((PADDING, y), line, font=title_font, fill=theme["text"])
        y += 48

    y += 12
    if item.rewards:
        reward_line = f"Recompensa: {item.rewards}"
        for line in _wrap_text(draw, reward_line, label_font, content_w, max_lines=1):
            draw.text((PADDING, y), line, font=label_font, fill=theme["highlight"])
            y += 30

    max_body_lines = 3 if (item.promo_code and item.rewards) else (4 if item.promo_code else 6)
    for line in _wrap_text(draw, item.summary, body_font, content_w, max_lines=max_body_lines):
        draw.text((PADDING, y), line, font=body_font, fill=theme["muted"])
        y += 34

    if item.promo_code:
        box_top = 382
        box_bottom = 480
        draw.rounded_rectangle(
            (PADDING, box_top, CARD_WIDTH - PADDING, box_bottom),
            radius=18,
            fill="#0B1220",
            outline=theme["highlight"],
            width=3,
        )
        draw.text(
            (PADDING + 22, box_top + 12),
            "CÓDIGO EXACTO (TOCA EL TEXTO DEL MENSAJE PARA COPIAR):",
            font=label_font,
            fill=theme["highlight"],
        )
        code_font = _fit_single_line_font(draw, item.promo_code, "mono", content_w - 44)
        draw.text(
            (PADDING + 22, box_top + 42),
            item.promo_code,
            font=code_font,
            fill="#FFFFFF",
        )

    # Pie informativo
    draw.line((PADDING, 512, CARD_WIDTH - PADDING, 512), fill="#334155", width=2)
    left_footer = f"Vigencia / Fecha: {item.expires_at}" if item.expires_at else "Estado: Vigente / Reciente"
    right_footer = f"Fuente: {item.source_name}"

    if draw.textlength(left_footer, font=footer_font) > content_w - 260:
        left_footer = _wrap_text(draw, left_footer, footer_font, content_w - 260, 1)[0]

    draw.text((PADDING, 530), left_footer, font=footer_font, fill=theme["muted"])
    right_w = draw.textlength(right_footer, font=footer_font)
    draw.text((CARD_WIDTH - PADDING - right_w, 530), right_footer, font=footer_font, fill=theme["muted"])

    img.save(output_path, format="PNG", optimize=True)
    return output_path
