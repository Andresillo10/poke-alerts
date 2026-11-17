"""Glosario curado de jerga oficial en español para Pokémon GO y Pokémon TCG Pocket."""

from __future__ import annotations

import re

# Términos técnicos donde la traducción automática suele errar la jerga oficial.
OFFICIAL_TERMS_ES: dict[str, str] = {
    "Wonder Pick Event": "Evento de Elección Mágica",
    "Wonder Pick": "Elección Mágica",
    "Mass Outbreak Event": "Evento de Aparición Masiva",
    "Mass Outbreak": "Aparición Masiva",
    "Ranked Match Season": "Temporada de Combates Clasificatorios",
    "Community Week Missions": "Misiones de Semana de la Comunidad",
    "Bonus Week Missions": "Misiones de Semana de Bonificación",
    "Elite Deck Gift Missions": "Misiones de Regalo de Mazo Élite",
    "Emblem Event": "Evento de Emblemas",
    "Drop Event": "Evento de Botín",
    "Limited Battles": "Combates Limitados",
    "Solo Battle Events": "Eventos de Combate Individual",
    "Solo Battles": "Combates Individuales",
    "Pack Hourglasses": "Relojes de Arena de Sobres",
    "Pack Hourglass": "Reloj de Arena de Sobres",
    "Wonder Hourglasses": "Relojes de Arena Mágicos",
    "Wonder Hourglass": "Reloj de Arena Mágico",
    "Trade Tokens": "Fichas de Intercambio",
    "Trade Token": "Ficha de Intercambio",
    "Promo Pack": "Sobre Promo",
    "Promo Cards": "Cartas Promo",
    "Promo Card": "Carta Promo",
    "Community Day": "Día de la Comunidad",
    "Raid Day": "Día de Incursiones",
    "Research Day": "Día de Investigación",
    "Hatch Day": "Día de Eclosiones",
    "Incense Day": "Día de Incienso",
    "Max Battle Day": "Día de Combates Max",
    "Max Monday": "Lunes Max",
    "Spotlight Hour": "Hora del Pokémon Destacado",
    "Raid Hour": "Hora de Incursiones",
    "Timed Research": "Investigación Temporal",
    "Field Research": "Investigación de Campo",
    "Special Research": "Investigación Especial",
    "Max Mushroom": "Maxiseta",
    "Max Mushrooms": "Maxisetas",
    "Razz Berry": "Baya Frambu",
    "Pinap Berry": "Baya Pinia",
    "Nanab Berry": "Baya Latano",
}

POST_TRANSLATION_FIXES: dict[str, str] = {
    "Baya Razz": "Baya Frambu",
    "Baya Pinap": "Baya Pinia",
    "Baya Nanab": "Baya Latano",
    "Hongo máximo": "Maxiseta",
    "Hongos máximos": "Maxisetas",
}

_SORTED_TERMS = sorted(OFFICIAL_TERMS_ES.keys(), key=len, reverse=True)
GLOSSARY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(term) for term in _SORTED_TERMS) + r")\b",
    flags=re.IGNORECASE,
)
_LOWER_LOOKUP = {k.lower(): v for k, v in OFFICIAL_TERMS_ES.items()}

_SORTED_POST = sorted(POST_TRANSLATION_FIXES.keys(), key=len, reverse=True)
POST_FIX_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(term) for term in _SORTED_POST) + r")\b",
    flags=re.IGNORECASE,
)
_POST_LOOKUP = {k.lower(): v for k, v in POST_TRANSLATION_FIXES.items()}


def lookup_spanish_term(matched_text: str) -> str:
    """Devuelve el término oficial en español para una coincidencia del glosario."""
    return _LOWER_LOOKUP.get(matched_text.lower(), matched_text)


def apply_post_translation_fixes(text: str) -> str:
    """Corrige nombres oficiales de objetos/bayas tras la traducción automática."""
    return POST_FIX_PATTERN.sub(lambda m: _POST_LOOKUP.get(m.group(0).lower(), m.group(0)), text)
