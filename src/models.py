"""Modelos de datos y normalización para deduplicación entre fuentes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal

GameType = Literal["pogo", "tcgp"]
AlertKind = Literal["code", "event", "news"]

_BOILERPLATE_RE = re.compile(
    r"\b(ranking guide|all rewards|guide|event|events|evento|eventos|leek duck|ptcgpocket)\b"
)
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
    clean = _BOILERPLATE_RE.sub(" ", clean)
    clean = _NON_ALNUM_RE.sub(" ", clean).strip()
    tokens = [tok for tok in clean.split() if len(tok) > 1 or tok.isdigit()]
    slug = "-".join(tokens[:8]) or item.uid
    return f"{item.game}:{slug}"
