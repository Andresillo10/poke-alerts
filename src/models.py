"""Modelos de datos y normalización para deduplicación entre fuentes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

_MONTH_NUMS = {
    "january": 1, "jan": 1, "enero": 1, "ene": 1,
    "february": 2, "feb": 2, "febrero": 2,
    "march": 3, "mar": 3, "marzo": 3,
    "april": 4, "apr": 4, "abril": 4, "abr": 4,
    "may": 5, "mayo": 5,
    "june": 6, "jun": 6, "junio": 6,
    "july": 7, "jul": 7, "julio": 7,
    "august": 8, "aug": 8, "agosto": 8, "ago": 8,
    "september": 9, "sep": 9, "septiembre": 9,
    "october": 10, "oct": 10, "octubre": 10,
    "november": 11, "nov": 11, "noviembre": 11,
    "december": 12, "dec": 12, "diciembre": 12, "dic": 12,
}
_MONTH_ES_SHORT = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TEXT_DATE_RE = re.compile(r"\b([A-Za-záéíóúÁÉÍÓÚ]{3,10})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)


def parse_date_flex(raw: str | None) -> date | None:
    """Convierte fechas ISO o cadenas en inglés ('January 15, 2027') a date."""
    if not raw:
        return None
    iso_m = _ISO_DATE_RE.search(raw)
    if iso_m:
        try:
            return date.fromisoformat(iso_m.group(1))
        except ValueError:
            pass
    txt_m = _TEXT_DATE_RE.search(raw)
    if txt_m:
        m_num = _MONTH_NUMS.get(txt_m.group(1).lower())
        if m_num:
            try:
                return date(int(txt_m.group(3)), m_num, int(txt_m.group(2)))
            except ValueError:
                return None
    return None


def format_date_es(d: date | None) -> str:
    """Formatea una fecha compacta legible en español ('15 ene 2027')."""
    if not d:
        return ""
    return f"{d.day:02d} {_MONTH_ES_SHORT[d.month]} {d.year}"


def format_item_dates_es(starts_on: str | None, expires_at: str | None) -> str:
    """Formatea las fechas de una alerta a español legible sin rangos ISO duplicados."""
    if not expires_at:
        s_dt = parse_date_flex(starts_on)
        return format_date_es(s_dt) if s_dt else ""
    isos = _ISO_DATE_RE.findall(expires_at)
    if len(isos) >= 2:
        d1, d2 = parse_date_flex(isos[0]), parse_date_flex(isos[1])
        if d1 and d2:
            return format_date_es(d1) if d1 == d2 else f"Del {format_date_es(d1)} al {format_date_es(d2)}"
    if len(isos) == 1 or re.search(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|aug|sep|oct|nov|dec)\b", expires_at, re.I):
        d = parse_date_flex(expires_at)
        if d:
            return format_date_es(d)
    return expires_at

GameType = Literal["pogo", "tcgp"]
AlertKind = Literal["code", "event", "news"]

_BOILERPLATE_RE = re.compile(
    r"\b(ranking guide|all rewards|guide|missions?|mision(?:es)?|event|events|evento|eventos|leek duck|ptcgpocket)\b"
)
_PAREN_SET_RE = re.compile(r"\([a-c]\d+[a-z]?\)")
_SET_CODE_TOKEN_RE = re.compile(r"^[a-c]\d+[a-z]?$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class AlertItem:
    """Representa una alerta normalizada lista para enriquecer, traducir, renderizar y enviar."""

    uid: str
    game: GameType
    kind: AlertKind
    title: str
    summary: str
    url: str
    source_name: str
    promo_code: str | None = None
    redeem_url: str | None = None
    rewards: str | None = None
    expires_at: str | None = None
    language: str = "en"
    guide_bullets: tuple[str, ...] = ()
    deck_guide_url: str | None = None
    deck_guide_title: str | None = None
    starts_on: str | None = None
    is_launch_reminder: bool = False

    def with_spanish_copy(
        self,
        *,
        title: str,
        summary: str,
        rewards: str | None = None,
        expires_at: str | None = None,
        guide_bullets: tuple[str, ...] | None = None,
        deck_guide_title: str | None = None,
    ) -> "AlertItem":
        """Devuelve una copia del item conservando intactos código y URLs."""
        return replace(
            self,
            title=title,
            summary=summary,
            rewards=rewards if rewards is not None else self.rewards,
            expires_at=expires_at if expires_at is not None else self.expires_at,
            guide_bullets=guide_bullets if guide_bullets is not None else self.guide_bullets,
            deck_guide_title=(
                deck_guide_title if deck_guide_title is not None else self.deck_guide_title
            ),
            language="es",
        )


def _strip_accents(text: str) -> str:
    """Elimina tildes y diacríticos manteniendo caracteres ASCII base."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def build_topic_key(item: AlertItem) -> str:
    """Construye una clave canónica por tema para evitar duplicados entre portales."""
    if item.kind == "code" and item.promo_code:
        return f"{item.game}:code:{item.promo_code.strip().upper()}"

    clean = _strip_accents(item.title.lower())
    clean = _PAREN_SET_RE.sub(" ", clean)
    clean = _BOILERPLATE_RE.sub(" ", clean)
    clean = _NON_ALNUM_RE.sub(" ", clean).strip()
    tokens = [tok for tok in clean.split() if len(tok) > 1 or tok.isdigit()]
    slug = "-".join(tokens[:8]) or item.uid
    return f"{item.game}:{slug}"


def canonical_topic_signature(topic_key: str) -> str:
    """Firma independiente del orden de palabras para cruzar temas entre portales."""
    if ":" not in topic_key:
        return topic_key
    game, rest = topic_key.split(":", 1)
    if rest.startswith("code:"):
        return topic_key
    ignore = {"mission", "missions", "mision", "misiones", "guide", "event", "events"}
    base_tokens = [tok for tok in rest.split("-") if tok and tok not in ignore]
    no_set = [tok for tok in base_tokens if not _SET_CODE_TOKEN_RE.match(tok)]
    chosen = no_set if len(no_set) >= 3 else base_tokens
    return f"{game}:{'-'.join(sorted(set(chosen)))}"
