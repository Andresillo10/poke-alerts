"""Traductor ligero EN -> ES con protección de códigos alfanuméricos y glosario Pokémon."""

from __future__ import annotations

import logging
import re

import requests

from src.config import REQUEST_TIMEOUT_SEC
from src.glossary import GLOSSARY_PATTERN, apply_post_translation_fixes, lookup_spanish_term
from src.models import AlertItem

LOGGER = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://\S+")


def _call_translate_api(text: str) -> str:
    """Traduce texto EN->ES usando deep-translator si existe, o endpoint ligero HTTP."""
    try:
        from deep_translator import GoogleTranslator

        translated = GoogleTranslator(source="en", target="es").translate(text)
        if translated:
            return translated
    except ImportError:
        pass
    except Exception as exc:
        LOGGER.warning("Fallo deep-translator (%s); probando fallback HTTP.", exc)

    resp = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": text},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    payload = resp.json()
    return "".join(segment[0] for segment in payload[0] if segment and segment[0])


def translate_en_to_es(
    text: str,
    *,
    protected_tokens: list[str] | None = None,
    enable_remote: bool = True,
) -> str:
    """Traduce al español protegiendo códigos exactos, URLs y términos del glosario."""
    clean = " ".join(text.split())
    if not clean:
        return ""

    replacements: dict[str, str] = {}

    def make_token(original_or_es: str) -> str:
        token = f"XPK{len(replacements)}X"
        replacements[token] = original_or_es
        return token

    working = clean
    for raw_code in protected_tokens or []:
        if raw_code:
            working = working.replace(raw_code, make_token(raw_code))

    working = _URL_RE.sub(lambda m: make_token(m.group(0)), working)
    working = GLOSSARY_PATTERN.sub(
        lambda m: make_token(lookup_spanish_term(m.group(0))),
        working,
    )

    if enable_remote:
        try:
            working = _call_translate_api(working)
        except Exception as exc:
            LOGGER.warning("No se pudo traducir remotamente (%s); usando glosario local.", exc)

    for token, value in replacements.items():
        working = working.replace(token, value)

    working = apply_post_translation_fixes(working)
    return " ".join(working.split())


def localize_alert_item(item: AlertItem, *, enable_remote: bool = True) -> AlertItem:
    """Localiza título, resumen, detalles v2.0 y guía de mazos sin alterar códigos."""
    if item.language == "es":
        if not item.deck_guide_title:
            return item
        return item.with_spanish_copy(
            title=item.title,
            summary=item.summary,
            deck_guide_title=translate_en_to_es(item.deck_guide_title, enable_remote=enable_remote),
        )

    protected = [item.promo_code] if item.promo_code else []
    title_es = translate_en_to_es(item.title, protected_tokens=protected, enable_remote=enable_remote)
    summary_es = translate_en_to_es(item.summary, protected_tokens=protected, enable_remote=enable_remote)
    rewards_es = (
        translate_en_to_es(item.rewards, protected_tokens=protected, enable_remote=enable_remote)
        if item.rewards
        else None
    )
    expires_es = (
        translate_en_to_es(item.expires_at, protected_tokens=protected, enable_remote=enable_remote)
        if item.expires_at
        else None
    )
    bullets_es = tuple(
        translate_en_to_es(b, protected_tokens=protected, enable_remote=enable_remote)
        for b in item.guide_bullets
    )
    deck_title_es = (
        translate_en_to_es(item.deck_guide_title, enable_remote=enable_remote)
        if item.deck_guide_title
        else None
    )
    return item.with_spanish_copy(
        title=title_es,
        summary=summary_es,
        rewards=rewards_es,
        expires_at=expires_es,
        guide_bullets=bullets_es,
        deck_guide_title=deck_title_es,
    )
