"""Glosario curado de jerga oficial en español para Pokémon GO y Pokémon TCG Pocket."""

from __future__ import annotations

import html
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
    "Daily Missions": "Misiones diarias",
    "Emblem Event": "Evento de Emblemas",
    "Drop Event": "Evento de Botín",
    "Limitado": "Limitado",
    "Limited Battles": "Combates Limitados",
    "Limited Battle": "Combate Limitado",
    "Step-Up Battle": "Combate Progresivo",
    "Versus Battles": "Combates Versus",
    "Solo Battle Events": "Eventos de Combate Individual",
    "Solo Battles": "Combates Individuales",
    "Solo Battle": "Combate Individual",
    "Best Decks": "Mejores Mazos",
    "Meta Tier List": "Tier List del Meta",
    "Tier List": "Tier List",
    "Pokémon TCG Pocket": "Pokémon TCG Pocket",
    "Pokémon GO": "Pokémon GO",
    "Pack Hourglasses": "Relojes de Arena de Sobres",
    "Pack Hourglass": "Reloj de Arena de Sobres",
    "Wonder Hourglasses": "Relojes de Arena Mágicos",
    "Wonder Hourglass": "Reloj de Arena Mágico",
    "Trade Tokens": "Fichas de Intercambio",
    "Trade Token": "Ficha de Intercambio",
    "Wonder Stamina": "Energía Mágica",
    "Shop Ticket": "Cupón de Tienda",
    "Card Sleeve": "Funda para Cartas",
    "Playmat": "Tapete",
    "Promo Card Exchange Ticket": "Cupón de Canje de Carta Promo",
    "Promo Card packs": "sobres de cartas promo",
    "Promo Card pack": "sobre de carta promo",
    "Promo Pack": "Sobre Promo",
    "Promo Cards": "Cartas Promo",
    "Promo Card": "Carta Promo",
    "booster packs": "sobres de cartas",
    "booster pack": "sobre de cartas",
    "pack stamina": "energía de sobres",
    "Increased Spawns": "Mayor aparición salvaje",
    "1/4 Egg Hatch Distance": "1/4 distancia para eclosionar Huevos",
    "1/2 Egg Hatch Distance": "1/2 distancia para eclosionar Huevos",
    "2× Catch Candy": "2× Caramelos por captura",
    "2× Catch Stardust": "2× Polvo Estelar por captura",
    "2× Catch XP": "2× PX por captura",
    "3× Catch Stardust": "3× Polvo Estelar por captura",
    "3× Catch XP": "3× PX por captura",
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
    "Lucky Trade": "Intercambio con Suerte",
    "Lucky Friend": "Amistad con Suerte",
    "Lucky Friends": "Amistad con Suerte",
    "Lucky Pokémon": "Pokémon con Suerte",
    "Max Particles": "Partículas Max",
    "Power Spots": "Nodos Energéticos",
    "Catch Experience": "PX por captura",
    "Max Battles": "Combates Max",
    "Mega Evolution": "Megaevolución",
    "Mega Energy": "Megaenergía",
    "Catch Candy": "Caramelos por captura",
    "Wild spawns": "Pokémon salvajes",
    "Featured Pokémon": "Pokémon destacados",
    "Featured cards/packs": "Cartas y sobres destacados",
    "Shinies available": "Variocolor disponibles",
    "Bonuses": "Bonificaciones",
    "Missions": "Misiones",
    "Stardust": "Polvo Estelar",
    "Shinedust": "Polvo Iris",
    "Max Mushroom": "Maxiseta",
    "Max Mushrooms": "Maxisetas",
    "Razz Berry": "Baya Frambu",
    "Pinap Berry": "Baya Pinia",
    "Nanab Berry": "Baya Latano",
    "Classic Platform Clog Avatar Item": "Zueco con plataforma clásico (Artículo de avatar)",
    "Classic Clog Avatar Item": "Zueco clásico (Artículo de avatar)",
    "adidas Shoes Avatar Item": "Zapatos adidas (Artículo de avatar)",
    "Classic Platform Clog": "Zueco con plataforma clásico",
    "Classic Clog": "Zueco clásico",
    "adidas shoes": "Zapatos adidas",
    "Avatar Item": "Artículo de avatar",
    "in 5-star Raid Battles": "en Incursiones de 5 estrellas",
    "5-star Raid Battles": "Incursiones de 5 estrellas",
    "in Shadow Raids": "en Incursiones Oscuras",
    "Shadow Raids": "Incursiones Oscuras",
    "in Mega Raids": "en Megaincursiones",
    "Mega Raids": "Megaincursiones",
    "Great League": "Liga Súper",
    "Ultra League": "Liga Ultra",
    "Master League": "Liga Master",
    "Mega Edition": "Edición Mega",
    "Catch Cup": "Copa Captura",
    "Ranking Guide & All Rewards": "Guía de Rango y Recompensas",
    "Card List & Set Details": "Lista de Cartas y Detalles del Set",
    "Use pack stamina to open": "Abre sobres con energía:",
    "Participate in": "Participa en",
    "versus battle": "combate Versus",
    "3-hour Incense": "Incienso de 3 horas",
    "Incarnate Forme": "Forma Avatar",
    "January 2027": "enero de 2027",
    "February 2027": "febrero de 2027",
    "October 2026": "octubre de 2026",
    "November 2026": "noviembre de 2026",
    "December 2026": "diciembre de 2026",
    "October 2027": "octubre de 2027",
    "Log in": "Inicia sesión",
}

POST_TRANSLATION_FIXES: dict[str, str] = {
    "Baya Razz": "Baya Frambu",
    "Baya Pinap": "Baya Pinia",
    "Baya Nanab": "Baya Latano",
    "Hongo máximo": "Maxiseta",
    "Hongos máximos": "Maxisetas",
    "Polvo de brillo": "Polvo Iris",
    "Polvo brillante": "Polvo Iris",
    "Use energía de sobres to open": "Abre",
    "to open": "para abrir",
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
    clean = html.unescape(text).replace("Pok&émon", "Pokémon").replace("Pok&eacute;mon", "Pokémon")
    return POST_FIX_PATTERN.sub(lambda m: _POST_LOOKUP.get(m.group(0).lower(), m.group(0)), clean)
