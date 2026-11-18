"""Motor v3.0 de análisis de restricciones, validación de barajas y generación de QR TCG Pocket."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup

from src.config import REQUEST_TIMEOUT_SEC

LOGGER = logging.getLogger(__name__)

CARDS_DB_URL = (
    "https://raw.githubusercontent.com/PocketDecks/pokemon-tcg-pocket-cards/"
    "main/data/v5/cards.no-image.min.json"
)
EXPANSIONS_DB_URL = (
    "https://raw.githubusercontent.com/PocketDecks/pokemon-tcg-pocket-cards/"
    "main/data/v5/expansions.min.json"
)

TRAINER_ID_OFFSET = 10_000_000
DIAMOND_RARITIES = {"◊", "◊◊", "◊◊◊"}
CORE_PROMO_A_TRAINERS = {
    "Poké Ball",
    "Professor's Research",
    "Potion",
    "X Speed",
    "Hand Scope",
    "Pokédex",
    "Red Card",
}
CORE_TRAINER_PRIORITY = [
    "Professor's Research",
    "Poké Ball",
    "Serena's Choice",
    "Copycat",
    "Cyrus",
    "Sabrina",
    "Mars",
    "Red",
    "Leaf",
    "Giant Cape",
    "Rocky Helmet",
    "Rare Candy",
    "Potion",
    "X Speed",
    "Super Potion",
    "Red Card",
]

ENERGY_CHAR_TO_ID = {
    "G": 1,
    "R": 2,
    "W": 3,
    "L": 4,
    "P": 5,
    "F": 6,
    "D": 7,
    "M": 8,
}
SUBTYPE_TO_ENERGY_ID = {
    "Grass": 1,
    "Fire": 2,
    "Water": 3,
    "Lightning": 4,
    "Psychic": 5,
    "Fighting": 6,
    "Darkness": 7,
    "Metal": 8,
}
ENERGY_ID_LABELS_ES = {
    1: "Planta",
    2: "Fuego",
    3: "Agua",
    4: "Rayo",
    5: "Psíquico",
    6: "Lucha",
    7: "Oscuridad",
    8: "Metálico",
}
SUBTYPE_LABELS_ES = {
    "Grass": "Planta",
    "Fire": "Fuego",
    "Water": "Agua",
    "Lightning": "Rayo",
    "Psychic": "Psíquico",
    "Fighting": "Lucha",
    "Darkness": "Oscuridad",
    "Metal": "Metálico",
    "Dragon": "Dragón",
    "Colorless": "Incoloro",
}
_COMBAT_KEYWORDS = ("limited battle", "drop event", "emblem event", "ranked", "solo battle")
_NON_COMBAT_KEYWORDS = ("wonder pick", "mass outbreak")


def _norm_name(text: str) -> str:
    """Normaliza comillas y espacios en nombres de cartas."""
    return text.replace("\u2019", "'").replace("\u2018", "'").strip()


@dataclass(frozen=True)
class CardRecord:
    """Carta normalizada de Pokémon TCG Pocket con datos de combate e ID QR."""

    card_id: str
    name: str
    set_code: str
    card_type: str
    subtype: str
    stage: str | None
    evolves_from: str | None
    rarity: str
    is_ex: bool
    is_mega: bool
    hp: int
    retreat: int
    ability_name: str | None
    card_text: str
    attacks: tuple[tuple[str, int], ...]
    qr_id: int

    @property
    def is_pokemon(self) -> bool:
        return self.card_type == "Pokémon"

    @property
    def is_diamond_pokemon(self) -> bool:
        return self.is_pokemon and self.rarity in DIAMOND_RARITIES

    def score(self) -> float:
        """Puntaje táctico determinista por eficiencia de HP, daño y energía."""
        if not self.is_pokemon:
            return 10.0
        best_dmg = max((dmg for _, dmg in self.attacks), default=0)
        best_cost = min(
            (len(cost) for cost, dmg in self.attacks if dmg == best_dmg and cost),
            default=2,
        )
        efficiency = best_dmg / max(best_cost, 1)
        bonus = 18.0 if self.ability_name else 0.0
        if self.stage == "Basic" and self.is_ex:
            bonus += 22.0
        elif self.stage in {"Basic", "Stage 1"}:
            bonus += 12.0
        attack_types = {ch for cost, _ in self.attacks for ch in cost if ch in ENERGY_CHAR_TO_ID}
        if len(attack_types) <= 1:
            bonus += 18.0
        elif self.subtype != "Dragon":
            bonus -= 22.0
        retreat_penalty = self.retreat * 5.0
        return (self.hp * 0.35) + (best_dmg * 0.85) + (efficiency * 0.6) + bonus - retreat_penalty


@dataclass(frozen=True)
class EventDeckConstraints:
    """Restricciones y misiones extraídas de un evento de combate TCG Pocket."""

    is_combat_event: bool
    allowed_sets: tuple[str, ...] | None = None
    allowed_promo_names: tuple[str, ...] = ()
    no_ex: bool = False
    diamond_only: bool = False
    require_mega_ex: bool = False
    required_types: tuple[str, ...] = ()
    min_set_cards: tuple[str, int] | None = None
    target_expansion: str | None = None


@dataclass(frozen=True)
class BuiltDeck:
    """Baraja de 20 cartas validada con su payload QR listo para renderizar."""

    title: str
    mission_tag: str
    cards: tuple[CardRecord, ...]
    energy_types: tuple[int, ...]
    qr_payload: str

    def summary_lines(self) -> list[str]:
        """Devuelve líneas compactas `2× Carta` ordenadas (Pokémon primero)."""
        counts = Counter((c.is_pokemon, c.name) for c in self.cards)
        ordered = sorted(counts.items(), key=lambda kv: (not kv[0][0], -kv[1], kv[0][1]))
        return [f"{qty}× {name}" for (_, name), qty in ordered]


def encode_deck_payload(
    trainer_ids: list[int],
    pokemon_ids: list[int],
    energy_types: list[int],
) -> str:
    """Codifica una baraja al formato binario Base64 oficial de QR de TCG Pocket."""
    buf = bytearray([len(trainer_ids)])
    for tid in trainer_ids:
        val = tid + TRAINER_ID_OFFSET
        buf.extend([(val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF])
    buf.append(len(pokemon_ids))
    for pid in pokemon_ids:
        buf.extend([(pid >> 16) & 0xFF, (pid >> 8) & 0xFF, pid & 0xFF])
    buf.append(len(energy_types))
    buf.extend(energy_types)
    return base64.b64encode(bytes(buf)).decode("ascii")


def build_qr_download_url(qr_payload: str) -> str:
    """Genera un enlace HTTPS directo para descargar la imagen PNG de un solo QR."""
    encoded = urllib.parse.quote(qr_payload, safe="")
    return f"https://api.qrserver.com/v1/create-qr-code/?size=600x600&margin=24&format=png&download=1&data={encoded}"


def describe_deck_strategy(deck: BuiltDeck) -> str:
    """Genera una explicación táctica clara en español basada en cartas y entrenadores reales."""
    pks = [c for c in deck.cards if c.is_pokemon]
    trs = [c for c in deck.cards if not c.is_pokemon]
    if not pks:
        return f"Baraja equilibrada de 20 cartas ({len(trs)} Entrenadores)."

    unique_pks: dict[str, CardRecord] = {}
    for card in pks:
        unique_pks.setdefault(card.name, card)
    ordered_pks = sorted(
        unique_pks.values(),
        key=lambda c: (c.is_mega, c.is_ex, c.stage == "Stage 2", c.score()),
        reverse=True,
    )
    lead = ordered_pks[0]
    best_dmg = max((dmg for _, dmg in lead.attacks), default=0)
    supporters = [c for c in ordered_pks[1:3]]
    energy_txt = "/".join(ENERGY_ID_LABELS_ES.get(e, "?") for e in deck.energy_types)

    lead_stats = f"{lead.hp} HP" + (f", hasta {best_dmg} daño" if best_dmg > 0 else "")
    if lead.ability_name:
        lead_stats += f", hab. {lead.ability_name}"

    support_notes = []
    for sup in supporters:
        note = f"{sup.name} ({sup.ability_name})" if sup.ability_name else sup.name
        support_notes.append(note)
    extra = f" con apoyo de {', '.join(support_notes)}" if support_notes else ""

    tr_names = {c.name for c in trs}
    roles: list[str] = []
    if tr_names & {"Professor's Research", "Poké Ball", "Copycat", "Serena's Choice"}:
        roles.append("robo/búsqueda")
    if tr_names & {"Rare Candy", "Flame Patch", "Misty", "Irida"}:
        roles.append("aceleración")
    if tr_names & {"Sabrina", "Cyrus", "Mars"}:
        roles.append("presión al rival")
    if tr_names & {"Giant Cape", "Eviolite", "Potion", "Super Potion", "Rocky Helmet"}:
        roles.append("aguante")
    plan = f" Motor: {', '.join(roles)}." if roles else ""
    return f"Atacante clave {lead.name} ({lead_stats}) en energía {energy_txt}{extra}.{plan}"


def decode_deck_payload(payload: str) -> tuple[list[int], list[int], list[int]]:
    """Decodifica un payload QR de TCG Pocket a `(trainer_ids, pokemon_ids, energy_types)`."""
    buf = base64.b64decode(payload)
    idx = 0
    t_cnt = buf[idx]
    idx += 1
    trainers = []
    for _ in range(t_cnt):
        val = (buf[idx] << 16) | (buf[idx + 1] << 8) | buf[idx + 2]
        trainers.append(val - TRAINER_ID_OFFSET)
        idx += 3
    p_cnt = buf[idx]
    idx += 1
    pokemon = []
    for _ in range(p_cnt):
        val = (buf[idx] << 16) | (buf[idx + 1] << 8) | buf[idx + 2]
        pokemon.append(val)
        idx += 3
    e_cnt = buf[idx]
    idx += 1
    energies = list(buf[idx : idx + e_cnt])
    return trainers, pokemon, energies


def _to_qr_id(raw_card: dict[str, Any]) -> int | None:
    """Convierte `deckBuilderNr` de PocketDecks al `entityId` exacto del QR."""
    nr = raw_card.get("deckBuilderNr")
    if not isinstance(nr, int):
        return None
    if raw_card.get("type") == "Pokémon":
        return nr * 10
    return (nr - 1_000_000) * 10


class CardCatalog:
    """Catálogo en memoria de todas las cartas jugables de Pokémon TCG Pocket."""

    def __init__(self, raw_cards: list[dict[str, Any]], expansions: list[dict[str, Any]]) -> None:
        self.expansions = expansions
        exp_pairs = [
            (str(e.get("id") or e.get("code") or "").lower(), str(e.get("name") or ""))
            for e in expansions
        ]
        self.latest_set = next(
            (code for code, _ in reversed(exp_pairs) if code and not code.startswith("p")),
            "c1",
        )
        self.expansion_by_name = {
            name.lower(): code for code, name in exp_pairs if code and name
        }
        self.cards: list[CardRecord] = []
        self.by_name: dict[str, list[CardRecord]] = {}
        self.by_set_name: dict[tuple[str, str], CardRecord] = {}
        self.by_card_id: dict[str, CardRecord] = {}
        self._load(raw_cards)

    def _load(self, raw_cards: list[dict[str, Any]]) -> None:
        seen_entities: set[tuple[bool, int]] = set()
        for raw in raw_cards:
            qr_id = _to_qr_id(raw)
            if qr_id is None or qr_id <= 0:
                continue
            is_pk = raw.get("type") == "Pokémon"
            # Prioriza la impresión estándar sobre versiones shiny/full-art duplicadas
            if (is_pk, qr_id) in seen_entities and raw.get("shiny"):
                continue

            attacks: list[tuple[str, int]] = []
            for atk in (raw.get("attacks") or {}).values():
                if not atk or not atk.get("cost"):
                    continue
                dmg_raw = atk.get("damage") or 0
                try:
                    dmg = int(str(dmg_raw).rstrip("+x×")) if dmg_raw else 0
                except ValueError:
                    dmg = 0
                if dmg == 0 and atk.get("effect"):
                    m_eff = re.search(r"(\d+)\s+damage", str(atk["effect"]), re.I)
                    if m_eff:
                        dmg = int(m_eff.group(1))
                attacks.append((str(atk["cost"]), dmg))

            ab = raw.get("ability") or {}
            rec = CardRecord(
                card_id=str(raw.get("id", "")),
                name=_norm_name(str(raw.get("name", ""))),
                set_code=str(raw.get("set_code", "")).lower(),
                card_type=str(raw.get("type", "")),
                subtype=str(raw.get("subtype", "")),
                stage=raw.get("stage"),
                evolves_from=_norm_name(raw["evolves_from"]) if raw.get("evolves_from") else None,
                rarity=str(raw.get("rarity") or ""),
                is_ex=bool(raw.get("ex")),
                is_mega=bool(raw.get("mega")),
                hp=int(raw.get("health") or 0),
                retreat=int(raw.get("retreat") or 0),
                ability_name=ab.get("name") if ab.get("exists") else None,
                card_text=str(raw.get("card_text") or ""),
                attacks=tuple(attacks),
                qr_id=qr_id,
            )
            if (is_pk, qr_id) not in seen_entities:
                seen_entities.add((is_pk, qr_id))
                self.cards.append(rec)
                self.by_name.setdefault(rec.name.lower(), []).append(rec)
            self.by_set_name.setdefault((rec.set_code, rec.name.lower()), rec)
            if rec.card_id:
                self.by_card_id.setdefault(rec.card_id.lower(), rec)


def load_catalog(session: requests.Session) -> CardCatalog:
    """Descarga el catálogo oficial comunitario v5 (sin imágenes, ~4.4 MB)."""
    raw_cards = session.get(CARDS_DB_URL, timeout=max(REQUEST_TIMEOUT_SEC, 25)).json()
    raw_exps = session.get(EXPANSIONS_DB_URL, timeout=REQUEST_TIMEOUT_SEC).json()
    return CardCatalog(raw_cards, raw_exps)


def extract_event_constraints(
    *,
    title: str,
    detail_html: str,
    catalog: CardCatalog,
    extra_text: str = "",
) -> EventDeckConstraints:
    """Detecta si el evento exige combates y extrae sus restricciones reales."""
    lower_title = title.lower()
    if any(k in lower_title for k in _NON_COMBAT_KEYWORDS):
        return EventDeckConstraints(is_combat_event=False)

    soup = BeautifulSoup(detail_html, "html.parser")
    main = soup.select_one("main") or soup
    body_text = " ".join(main.get_text(" ", strip=True).split()) + " " + extra_text
    lower_body = body_text.lower()

    is_combat = any(k in lower_title or k in lower_body for k in _COMBAT_KEYWORDS)
    if not is_combat:
        return EventDeckConstraints(is_combat_event=False)

    detected_sets: list[str] = []
    for exp_name, code in catalog.expansion_by_name.items():
        if re.search(
            rf"\b(?:only use cards from|only cards from|usable expansions?\s*:?)\b[^.]*\b{re.escape(exp_name)}\b",
            lower_body,
        ):
            detected_sets.append(code)

    min_set_cards: tuple[str, int] | None = None
    for m_min in re.finditer(
        r"(\d+)\s+or more\s+(?:cards from\s+(?:the\s+)?)?([A-Za-z0-9:'\s-]+)",
        body_text,
        re.I,
    ):
        qty = int(m_min.group(1))
        frag = m_min.group(2).lower()
        for exp_name, code in catalog.expansion_by_name.items():
            if exp_name in frag:
                min_set_cards = (code, qty)
                break
        if min_set_cards:
            break

    target_exp = detected_sets[0] if detected_sets else (min_set_cards[0] if min_set_cards else None)
    if not target_exp:
        for exp_name, code in catalog.expansion_by_name.items():
            if exp_name in lower_title:
                target_exp = code
                break

    if not detected_sets and target_exp and ("limited battle" in lower_title or "limited battle event" in lower_body):
        detected_sets = [target_exp]

    promo_names: list[str] = []
    for tbl in main.select("table.dextab, table.dextable"):
        for a in tbl.select("a"):
            txt = _norm_name(a.get_text(" ", strip=True))
            if txt and txt.lower() in catalog.by_name:
                promo_names.append(txt)
    if not promo_names:
        for img in main.select("img[alt]"):
            alt = str(img.get("alt") or "")
            for tok in re.findall(r"[A-Za-z]{4,}", alt):
                if tok.lower() in catalog.by_name:
                    promo_names.append(catalog.by_name[tok.lower()][0].name)

    no_ex = "without any pokémon ex" in lower_body or "no pokémon ex" in lower_body
    diamond_only = "◊, ◊◊, or ◊◊◊" in body_text or "1, 2, or 3 diamond" in lower_body
    require_mega = "mega evolution" in lower_body

    req_types: list[str] = []
    for t_name in SUBTYPE_TO_ENERGY_ID:
        if re.search(rf"\b{t_name.lower()}-type pok[eé]mon\b|\bonly {t_name.lower()}\b", lower_body):
            req_types.append(t_name)
    if re.search(r"\bdragon-type pok[eé]mon\b", lower_body):
        req_types.append("Dragon")

    return EventDeckConstraints(
        is_combat_event=True,
        allowed_sets=tuple(detected_sets) if detected_sets else None,
        allowed_promo_names=tuple(dict.fromkeys(promo_names)),
        no_ex=no_ex,
        diamond_only=diamond_only,
        require_mega_ex=require_mega,
        required_types=tuple(dict.fromkeys(req_types)),
        min_set_cards=min_set_cards,
        target_expansion=target_exp or catalog.latest_set,
    )


def infer_energy_types(pokemon_cards: list[CardRecord]) -> list[int]:
    """Calcula las energías seleccionables (1..8) requeridas por los ataques del mazo."""
    used: list[int] = []
    for card in pokemon_cards:
        for cost, _ in card.attacks:
            for ch in cost:
                eid = ENERGY_CHAR_TO_ID.get(ch)
                if eid and eid not in used:
                    used.append(eid)
    if not used:
        for card in pokemon_cards:
            if card.subtype in SUBTYPE_TO_ENERGY_ID:
                eid = SUBTYPE_TO_ENERGY_ID[card.subtype]
                if eid not in used:
                    used.append(eid)
    return used[:3] if used else [5]


def _is_card_allowed(card: CardRecord, constraints: EventDeckConstraints) -> bool:
    """Verifica si una carta cumple whitelist de expansión y promos del evento."""
    if not constraints.allowed_sets:
        return True
    if card.set_code in constraints.allowed_sets:
        return True
    if not card.is_pokemon and card.name in CORE_PROMO_A_TRAINERS:
        return True
    return card.name in constraints.allowed_promo_names


def validate_deck(
    cards: list[CardRecord],
    energy_types: list[int],
    constraints: EventDeckConstraints,
    *,
    require_no_ex: bool = False,
    require_diamond: bool = False,
    require_mega: bool = False,
    only_type: str | None = None,
    min_set: tuple[str, int] | None = None,
) -> tuple[bool, str]:
    """Audita estrictamente las 20 cartas, evoluciones, copias y reglas del evento."""
    if len(cards) != 20:
        return False, f"Total inválido: {len(cards)} cartas"
    if not energy_types or len(energy_types) > 3 or any(e not in range(1, 9) for e in energy_types):
        return False, f"Energías inválidas: {energy_types}"

    name_counts = Counter(c.name.lower() for c in cards)
    if any(cnt > 2 for cnt in name_counts.values()):
        return False, "Supera 2 copias de una misma carta"

    pokemon = [c for c in cards if c.is_pokemon]
    if not any(c.stage == "Basic" for c in pokemon):
        return False, "Falta al menos 1 Pokémon Básico"

    present_names = {c.name.lower() for c in pokemon}
    for card in pokemon:
        if not _is_card_allowed(card, constraints):
            return False, f"Carta fuera de restricción: {card.name}"
        if card.stage in {"Stage 1", "Stage 2"}:
            if not card.evolves_from or card.evolves_from.lower() not in present_names:
                return False, f"Falta pre-evolución {card.evolves_from} para {card.name}"
        if require_no_ex and (card.is_ex or card.is_mega):
            return False, f"Contiene ex/Mega prohibido: {card.name}"
        if require_diamond and not card.is_diamond_pokemon:
            return False, f"No es rareza 1-3◊: {card.name}"
        if only_type and card.subtype != only_type:
            return False, f"Tipo {card.subtype} != {only_type}"

    for card in cards:
        if not card.is_pokemon and not _is_card_allowed(card, constraints):
            return False, f"Entrenador fuera de restricción: {card.name}"

    if require_mega and not any(c.is_mega for c in pokemon):
        return False, "Falta al menos 1 Mega Evolution ex"

    if min_set:
        code, minimum = min_set
        count_set = sum(1 for c in cards if c.set_code == code)
        if count_set < minimum:
            return False, f"Solo {count_set}/{minimum} cartas de {code}"

    return True, "OK"


def _build_evolution_core(
    anchor: CardRecord,
    pool: list[CardRecord],
) -> list[CardRecord]:
    """Construye una línea evolutiva completa `2×` desde el Pokémon elegido."""
    by_name = {c.name.lower(): c for c in pool if c.is_pokemon}
    chain: list[CardRecord] = [anchor]
    curr = anchor
    while curr.stage in {"Stage 1", "Stage 2"} and curr.evolves_from:
        prev = by_name.get(curr.evolves_from.lower())
        if not prev:
            return []
        chain.append(prev)
        curr = prev
    if curr.stage != "Basic":
        return []
    result: list[CardRecord] = []
    for c in reversed(chain):
        result.extend([c, c])
    return result


def _fill_trainers(
    pokemon_cards: list[CardRecord],
    trainer_pool: list[CardRecord],
    target_total: int = 20,
    prefer_set: str | None = None,
    prioritize_set: bool = False,
) -> list[CardRecord]:
    """Completa hasta 20 cartas eligiendo los mejores Entrenadores legales."""
    needed = target_total - len(pokemon_cards)
    if needed <= 0:
        return []

    pk_names = {c.name.lower() for c in pokemon_cards}
    has_basic = any(c.stage == "Basic" for c in pokemon_cards)
    has_stage2 = any(c.stage == "Stage 2" for c in pokemon_cards)

    def trainer_rank(tr: CardRecord) -> tuple[int, int]:
        txt = tr.card_text.lower()
        score = 0
        if tr.name in CORE_TRAINER_PRIORITY:
            score += 100 - CORE_TRAINER_PRIORITY.index(tr.name) * 4
        if tr.name == "Rare Candy":
            score += 140 if has_stage2 else -500
        if "pikachu ex" in txt and "pikachu ex" in pk_names:
            score += 95
        if "mega mewtwo y ex" in txt and "mega mewtwo y ex" in pk_names:
            score += 95
        if any(n in txt for n in pk_names if len(n) > 4):
            score += 80
        if "eviolite" in tr.name.lower() and has_basic:
            score += 75
        if prefer_set and tr.set_code == prefer_set:
            score += 130 if prioritize_set else 25
        return (score, 1 if tr.subtype == "Supporter" else 0)

    ranked = sorted(trainer_pool, key=trainer_rank, reverse=True)
    chosen: list[CardRecord] = []
    used_names: set[str] = set()
    for tr in ranked:
        if tr.name == "Rare Candy" and not has_stage2:
            continue
        if tr.name.lower() in used_names:
            continue
        if len(chosen) + 2 <= needed:
            chosen.extend([tr, tr])
            used_names.add(tr.name.lower())
        elif len(chosen) + 1 <= needed:
            chosen.append(tr)
            used_names.add(tr.name.lower())
        if len(chosen) == needed:
            break
    return chosen


def _distinct_energy_count(pokemon_cards: list[CardRecord]) -> int:
    """Cuenta cuántas energías base únicas exigen los ataques de los Pokémon."""
    ids = {
        ENERGY_CHAR_TO_ID[ch]
        for card in pokemon_cards
        for cost, _ in card.attacks
        for ch in cost
        if ch in ENERGY_CHAR_TO_ID
    }
    return len(ids)


def _build_single_archetype_deck(
    *,
    title: str,
    mission_tag: str,
    pokemon_pool: list[CardRecord],
    trainer_pool: list[CardRecord],
    constraints: EventDeckConstraints,
    prefer_subtype: str | None = None,
    prefer_set: str | None = None,
    prefer_fast: bool = False,
    require_no_ex: bool = False,
    require_diamond: bool = False,
    require_mega: bool = False,
    min_set: tuple[str, int] | None = None,
    exclude_anchors: set[str] | None = None,
) -> BuiltDeck | None:
    """Construye y valida una baraja de 20 cartas alrededor de un atacante principal."""
    excluded = exclude_anchors or set()
    candidates = [
        c
        for c in pokemon_pool
        if c.name not in excluded
        and (not prefer_subtype or c.subtype == prefer_subtype)
        and (not require_no_ex or (not c.is_ex and not c.is_mega))
        and (not require_diamond or c.is_diamond_pokemon)
        and (not require_mega or c.is_mega)
    ]
    effective_set = min_set[0] if min_set else prefer_set

    def _is_fast_attacker(card: CardRecord) -> bool:
        min_cost = min((len(cost) for cost, _ in card.attacks if cost), default=4)
        e_types = {ch for cost, _ in card.attacks for ch in cost if ch in ENERGY_CHAR_TO_ID}
        return card.stage in {"Basic", "Stage 1"} and min_cost <= 3 and len(e_types) <= 1

    candidates.sort(
        key=lambda c: (
            (1 if prefer_fast and _is_fast_attacker(c) else 0),
            (1 if min_set and effective_set and c.set_code == effective_set else 0),
            c.score() + (12.0 if not min_set and effective_set and c.set_code == effective_set else 0.0),
        ),
        reverse=True,
    )

    for anchor in candidates[:12]:
        core = _build_evolution_core(anchor, pokemon_pool)
        if not core or len(core) > 10:
            continue

        # Añade un segundo atacante Básico ágil (retreat <= 2) sin romper mono-energía
        pokemon = list(core)
        compatible_subtypes = (anchor.subtype, "Colorless") if anchor.subtype == "Dragon" else (anchor.subtype,)
        max_energies = 2 if anchor.subtype == "Dragon" else 1
        support_pool = [
            c
            for c in pokemon_pool
            if c.stage == "Basic"
            and c.name != anchor.name
            and c.name not in {p.name for p in pokemon}
            and c.subtype in compatible_subtypes
            and c.retreat <= 2
            and (not require_no_ex or (not c.is_ex and not c.is_mega))
            and (not require_diamond or c.is_diamond_pokemon)
        ]
        support_pool.sort(
            key=lambda c: (1 if effective_set and c.set_code == effective_set else 0, c.score()),
            reverse=True,
        )
        for sup in support_pool:
            if len(pokemon) + 2 <= 10 and _distinct_energy_count(pokemon + [sup]) <= max_energies:
                pokemon.extend([sup, sup])
                break

        trainers = _fill_trainers(
            pokemon,
            trainer_pool,
            prefer_set=effective_set,
            prioritize_set=bool(min_set),
        )
        deck_cards = pokemon + trainers
        energies = infer_energy_types(pokemon)
        ok, _ = validate_deck(
            deck_cards,
            energies,
            constraints,
            require_no_ex=require_no_ex,
            require_diamond=require_diamond,
            require_mega=require_mega,
            min_set=min_set,
        )
        if ok:
            payload = encode_deck_payload(
                [c.qr_id for c in trainers],
                [c.qr_id for c in pokemon],
                energies,
            )
            return BuiltDeck(
                title=title,
                mission_tag=mission_tag,
                cards=tuple(deck_cards),
                energy_types=tuple(energies),
                qr_payload=payload,
            )
    return None


def _try_gemini_decks(
    *,
    event_title: str,
    event_summary: str,
    constraints: EventDeckConstraints,
    catalog: CardCatalog,
    pokemon_pool: list[CardRecord],
    trainer_pool: list[CardRecord],
) -> list[BuiltDeck] | None:
    """Opcional ($0 Free Tier si GEMINI_API_KEY existe): diseña sinergias validadas."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    top_pk = sorted(pokemon_pool, key=lambda c: c.score(), reverse=True)[:45]
    top_tr = trainer_pool[:25]
    prompt = (
        f"Design 3 distinct 20-card Pokemon TCG Pocket decks in Spanish titles for event: "
        f"'{event_title}'. Context: {event_summary}. "
        f"Allowed Pokemon: {[c.name for c in top_pk]}. "
        f"Allowed Trainers: {[c.name for c in top_tr]}. "
        f"Return ONLY valid JSON array of 3 objects with keys: title, mission_tag, cards "
        f"(list of exact card names summing to 20, max 2 per name)."
    )
    try:
        model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        resp = requests.post(
            url,
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=20,
        )
        if not resp.ok:
            return None
        raw_txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        specs = json.loads(raw_txt)
        allowed_map = {c.name.lower(): c for c in pokemon_pool + trainer_pool}
        built: list[BuiltDeck] = []
        for spec in specs[:3]:
            cards = [allowed_map[_norm_name(n).lower()] for n in spec["cards"] if _norm_name(n).lower() in allowed_map]
            pks = [c for c in cards if c.is_pokemon]
            trs = [c for c in cards if not c.is_pokemon]
            energies = infer_energy_types(pks)
            ok, _ = validate_deck(cards, energies, constraints, min_set=constraints.min_set_cards)
            if ok:
                built.append(
                    BuiltDeck(
                        title=str(spec["title"])[:45],
                        mission_tag=str(spec["mission_tag"])[:45],
                        cards=tuple(cards),
                        energy_types=tuple(energies),
                        qr_payload=encode_deck_payload(
                            [c.qr_id for c in trs], [c.qr_id for c in pks], energies
                        ),
                    )
                )
        return built if len(built) == 3 else None
    except Exception as exc:
        LOGGER.warning("[Gemini Deck Builder] Usando fallback determinista ($0): %s", exc)
        return None


def build_three_decks_for_event(
    *,
    event_title: str,
    event_summary: str,
    constraints: EventDeckConstraints,
    catalog: CardCatalog,
) -> list[BuiltDeck]:
    """Genera hasta 3 barajas 100% legales y validadas para un evento de combate."""
    pokemon_pool = [c for c in catalog.cards if c.is_pokemon and _is_card_allowed(c, constraints)]
    trainer_pool = [c for c in catalog.cards if not c.is_pokemon and _is_card_allowed(c, constraints)]

    gemini_decks = _try_gemini_decks(
        event_title=event_title,
        event_summary=event_summary,
        constraints=constraints,
        catalog=catalog,
        pokemon_pool=pokemon_pool,
        trainer_pool=trainer_pool,
    )
    if gemini_decks:
        return gemini_decks

    decks: list[BuiltDeck] = []
    used_anchors: set[str] = set()
    used_types: set[str] = set()
    target_set = constraints.target_expansion or catalog.latest_set

    # Baraja 1: cubre misión restrictiva (Sin ex / 1-3◊ / Tipo pedido) o mejor Aggro rápido
    pref_t1 = constraints.required_types[0] if constraints.required_types else None
    d1 = _build_single_archetype_deck(
        title="Opción 1: Sin Pokémon ex / Rápida" if constraints.no_ex or constraints.diamond_only else "Opción 1: Meta Rápida",
        mission_tag="Cumple: Sin ex / 1–3◊" if constraints.no_ex or constraints.diamond_only else "Alta velocidad y consistencia",
        pokemon_pool=pokemon_pool,
        trainer_pool=trainer_pool,
        constraints=constraints,
        prefer_subtype=pref_t1,
        prefer_set=target_set,
        prefer_fast=not constraints.required_types,
        require_no_ex=constraints.no_ex,
        require_diamond=constraints.diamond_only,
    )
    if not d1 and pref_t1:
        d1 = _build_single_archetype_deck(
            title="Opción 1: Sin Pokémon ex / Rápida" if constraints.no_ex or constraints.diamond_only else "Opción 1: Meta Rápida",
            mission_tag="Cumple: Sin ex / 1–3◊" if constraints.no_ex or constraints.diamond_only else "Alta velocidad y consistencia",
            pokemon_pool=pokemon_pool,
            trainer_pool=trainer_pool,
            constraints=constraints,
            prefer_set=target_set,
            require_no_ex=constraints.no_ex,
            require_diamond=constraints.diamond_only,
        )
    if d1:
        decks.append(d1)
        first_pk = next(c for c in d1.cards if c.is_pokemon)
        used_anchors.add(first_pk.name)
        used_types.add(first_pk.subtype)

    # Baraja 2: cubre Mega ex o segundo tipo elemental fuerte distinto
    alt_types = [
        t for t in ("Water", "Lightning", "Fire", "Psychic", "Fighting", "Grass", "Darkness", "Metal")
        if t not in used_types
    ]
    d2 = None
    for req_mega_flag in ([True, False] if constraints.require_mega_ex else [False]):
        for subtype in (list(constraints.required_types) + alt_types):
            d2 = _build_single_archetype_deck(
                title="Opción 2: Poder Mega / Control" if req_mega_flag else f"Opción 2: Control {SUBTYPE_LABELS_ES.get(subtype, subtype)}",
                mission_tag="Cumple: 1+ Mega ex" if req_mega_flag else "Control y aguante alto",
                pokemon_pool=pokemon_pool,
                trainer_pool=trainer_pool,
                constraints=constraints,
                prefer_subtype=subtype,
                prefer_set=target_set,
                require_mega=req_mega_flag,
                exclude_anchors=used_anchors,
            )
            if d2:
                break
        if d2:
            break
    if d2:
        decks.append(d2)
        first_pk = next(c for c in d2.cards if c.is_pokemon)
        used_anchors.add(first_pk.name)
        used_types.add(first_pk.subtype)

    # Baraja 3: temática de la expansión del evento (>=12 cartas si aplica)
    remaining_types = [t for t in alt_types if t not in used_types] + alt_types
    d3 = None
    min_set_req = constraints.min_set_cards
    set_label = min_set_req[0].upper() if min_set_req else target_set.upper()
    tag_3 = (
        f"Temática {set_label} (≥{min_set_req[1]} cartas)"
        if min_set_req
        else f"Sinergia expansión {target_set.upper()}"
    )
    for en_min in ([min_set_req, None] if min_set_req else [None]):
        for subtype in remaining_types:
            d3 = _build_single_archetype_deck(
                title=f"Opción 3: Temática {set_label}",
                mission_tag=tag_3 if en_min else f"Alternativa {SUBTYPE_LABELS_ES.get(subtype, subtype)}",
                pokemon_pool=pokemon_pool,
                trainer_pool=trainer_pool,
                constraints=constraints,
                prefer_subtype=subtype,
                prefer_set=min_set_req[0] if min_set_req else target_set,
                min_set=en_min,
                exclude_anchors=used_anchors,
            )
            if d3:
                break
        if d3:
            break
    if d3:
        decks.append(d3)

    return decks
