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
    "Max Repel",
    "Abandoned Station",
    "Red Card",
    "Potion",
    "X Speed",
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
    ability_text: str = ""
    attack_names: tuple[str, ...] = ()
    attack_texts: tuple[str, ...] = ()

    @property
    def is_pokemon(self) -> bool:
        return self.card_type == "Pokémon"

    @property
    def is_diamond_pokemon(self) -> bool:
        return self.is_pokemon and self.rarity in DIAMOND_RARITIES

    def energy_discard_severity(self) -> float:
        """Mide cuánta energía sacrifica el ataque principal sin recuperación."""
        if not self.is_pokemon or not self.attacks:
            return 0.0
        best_dmg = max((dmg for _, dmg in self.attacks), default=0)
        worst = 0.0
        for (cost, dmg), atk_txt in zip(self.attacks, self.attack_texts or ("",) * len(self.attacks)):
            low = atk_txt.lower()
            if "discard" not in low or "energy" not in low or "opponent" in low:
                continue
            if len(cost) <= 1 and dmg < best_dmg:
                continue
            if "discard all" in low or "discard 2" in low or "discard 3" in low:
                worst = max(worst, 2.0)
            else:
                worst = max(worst, 1.0)
        return worst

    def energy_ramp_subtype(self) -> str | None:
        """Devuelve el tipo elemental que acelera esta carta (Habilidad o ataque barato)."""
        if not self.is_pokemon:
            return None
        candidates: list[str] = []
        if self.ability_text:
            candidates.append(self.ability_text.lower())
        for (cost, _), atk_txt in zip(self.attacks, self.attack_texts or ("",) * len(self.attacks)):
            if len(cost) <= 2 and atk_txt:
                candidates.append(atk_txt.lower())
        for low in candidates:
            if "opponent" in low:
                continue
            if ("attach" in low or "produce" in low) and "energy" in low and (
                "energy zone" in low or "from your discard" in low or "from your deck" in low
            ):
                m_type = re.search(r"\[\s*([grwlpfmd])\s*\]", low, re.I)
                if m_type:
                    eid = ENERGY_CHAR_TO_ID.get(m_type.group(1).upper())
                    for st_name, st_eid in SUBTYPE_TO_ENERGY_ID.items():
                        if st_eid == eid:
                            return st_name
                return self.subtype if self.subtype in SUBTYPE_TO_ENERGY_ID else None
        return None

    def score(self) -> float:
        """Puntaje táctico determinista por eficiencia de HP, daño efectivo, sinergia y energía."""
        if not self.is_pokemon:
            return 10.0
        best_eff_dmg = 0.0
        best_cost = 2
        atk_txts = self.attack_texts or ("",) * len(self.attacks)
        for (cost, dmg), atk_txt in zip(self.attacks, atk_txts):
            eff_d = float(dmg)
            eff_c = max(len(cost), 1)
            low_t = atk_txt.lower()
            m_extra = re.search(r"at\s+least\s+(\d+)\s+extra", low_t)
            if m_extra:
                eff_c += int(m_extra.group(1))
            m_more = re.search(r"does\s+(\d+)\s+more\s+damage", low_t)
            if m_more:
                bonus_d = int(m_more.group(1))
                if "both of them are heads" in low_t:
                    eff_d += bonus_d * 0.25
                elif "until you get tails" in low_t:
                    eff_d += bonus_d * 1.0
                elif any(sc in low_t for sc in ("confused", "asleep", "paralyzed")):
                    eff_d += bonus_d * 0.35
                else:
                    eff_d += bonus_d * 0.70
            m_flip = re.search(r"flip\s+(\d+)\s+coins?", low_t)
            if m_flip and "for each heads" in low_t and not m_more and dmg > 0:
                eff_d = max(eff_d, int(m_flip.group(1)) * 0.5 * dmg)
            if (eff_d / eff_c) > (best_eff_dmg / max(best_cost, 1)) or (
                eff_d > best_eff_dmg and eff_c <= best_cost
            ):
                best_eff_dmg = eff_d
                best_cost = eff_c

        efficiency = best_eff_dmg / max(best_cost, 1)
        bonus = 18.0 if self.ability_name else 0.0
        if self.energy_ramp_subtype():
            bonus += 24.0
        if self.stage == "Basic" and self.is_ex:
            bonus += 22.0
        elif self.stage in {"Basic", "Stage 1"}:
            bonus += 14.0
        if self.is_mega and self.stage == "Stage 2":
            bonus -= 22.0
        if best_cost >= 4:
            bonus -= 28.0
        attack_types = {ch for cost, _ in self.attacks for ch in cost if ch in ENERGY_CHAR_TO_ID}
        if len(attack_types) <= 1:
            bonus += 18.0
        elif self.subtype != "Dragon":
            bonus -= 22.0
        retreat_penalty = self.retreat * 5.0
        discard_penalty = self.energy_discard_severity() * 26.0
        return (
            (self.hp * 0.35)
            + (best_eff_dmg * 0.68)
            + (efficiency * 1.12)
            + bonus
            - retreat_penalty
            - discard_penalty
        )


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
    """Genera una explicación táctica clara en español basada en sinergias reales del mazo."""
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
    pk_names = {n.lower() for n in unique_pks}
    tr_names = {c.name for c in trs}
    tr_low = {c.name.lower() for c in trs}
    energy_txt = "/".join(ENERGY_ID_LABELS_ES.get(e, "?") for e in deck.energy_types)

    # Explicaciones específicas de combos reales cuando el mazo los contiene
    if "raboot" in pk_names and ("lopunny" in pk_names or "azumarill" in pk_names):
        strikers = [n for n in ("Azumarill", "Lopunny") if n.lower() in pk_names]
        return (
            f"Combo Rabbit Jumping en energía {energy_txt}: Raboot en banca suma +3 monedas "
            f"por copia para disparar el daño de {' y '.join(strikers)}."
        )
    if "rika" in tr_low and ("paldean clodsire" in pk_names or "whiscash" in pk_names):
        return (
            f"Aceleración con Rika en energía {energy_txt}: carga 2 energías Lucha directo "
            "a Paldean Clodsire (130 daño) o Whiscash para atacar rápido."
        )
    if "blue's provocation" in tr_low and (pk_names & {"pidgeot", "charizard", "blastoise", "venusaur"}):
        unlocker_rec = next(
            unique_pks[n] for n in unique_pks if n.lower() in {"pidgeot", "charizard", "blastoise", "venusaur"}
        )
        chain_low = {unlocker_rec.name.lower()}
        cur = unlocker_rec
        by_low = {n.lower(): c for n, c in unique_pks.items()}
        while cur.evolves_from and cur.evolves_from.lower() in by_low:
            prev_rec = by_low[cur.evolves_from.lower()]
            chain_low.add(prev_rec.name.lower())
            cur = prev_rec
        partner = next(
            (c.name for c in ordered_pks if c.name.lower() not in chain_low and not (c.stage == "Basic" and any(o.evolves_from and o.evolves_from.lower() == c.name.lower() for o in ordered_pks))),
            "",
        )
        extra_p = f" junto a {partner}" if partner else ""
        return (
            f"Control en energía {energy_txt}: {unlocker_rec.name} habilita Blue's Provocation para "
            f"arrastrar amenazas de la banca rival{extra_p}."
        )
    if "mew ex" in pk_names and any("mega mewtwo" in n for n in pk_names):
        stadium_note = " y Abandoned Station aporta +40 de daño y robo" if "abandoned station" in tr_low else ""
        return (
            f"Motor Psíquico en energía {energy_txt}: Mew ex acelera energías hacia Mega Mewtwo "
            f"desde el inicio{stadium_note}."
        )

    lead = ordered_pks[0]
    best_dmg = max((dmg for _, dmg in lead.attacks), default=0)
    supporters = [c for c in ordered_pks[1:3]]
    lead_stats = f"{lead.hp} HP" + (f", hasta {best_dmg} daño" if best_dmg > 0 else "")
    if lead.ability_name:
        lead_stats += f", hab. {lead.ability_name}"

    support_notes = []
    for sup in supporters:
        note = f"{sup.name} ({sup.ability_name})" if sup.ability_name else sup.name
        support_notes.append(note)
    extra = f" con apoyo de {', '.join(support_notes)}" if support_notes else ""

    roles: list[str] = []
    if tr_names & {"Professor's Research", "Poké Ball", "Copycat", "Serena's Choice", "Abandoned Station"}:
        roles.append("robo/búsqueda")
    if (
        tr_names & {"Rare Candy", "Flame Patch", "Misty", "Irida", "Rika", "Genome Converter Y"}
        or any(c.energy_ramp_subtype() for c in unique_pks.values())
    ):
        roles.append("aceleración de energía")
    if tr_names & {"Sabrina", "Cyrus", "Mars", "Blue's Provocation", "Max Repel", "Red Card"}:
        roles.append("presión al rival")
    if tr_names & {"Giant Cape", "Potion", "Rocky Helmet", "Sweet Honey"}:
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
            atk_names: list[str] = []
            atk_texts: list[str] = []
            for atk in (raw.get("attacks") or {}).values():
                if not atk or not atk.get("cost"):
                    continue
                dmg_raw = atk.get("damage") or 0
                try:
                    dmg = int(str(dmg_raw).rstrip("+x×")) if dmg_raw else 0
                except ValueError:
                    dmg = 0
                eff_txt = str(atk.get("effect") or "")
                if dmg == 0 and eff_txt:
                    m_eff = re.search(r"(\d+)\s+damage", eff_txt, re.I)
                    if m_eff:
                        dmg = int(m_eff.group(1))
                attacks.append((str(atk["cost"]), dmg))
                atk_names.append(str(atk.get("name") or ""))
                atk_texts.append(eff_txt)

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
                ability_text=str(ab.get("effect") or "") if ab.get("exists") else "",
                attack_names=tuple(atk_names),
                attack_texts=tuple(atk_texts),
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
        if code.startswith("p"):
            continue
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
            href = str(a.get("href") or "")
            m_pc = re.search(r"/promo-([abc])/(\d{3})\.shtml", href, re.I)
            if m_pc:
                cid = f"p{m_pc.group(1).lower()}-{m_pc.group(2)}"
                rec = catalog.by_card_id.get(cid)
                if rec:
                    promo_names.append(rec.name)
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


_TRAINER_TARGET_PATTERNS = (
    re.compile(r"only if you have ([^.]+?) in play", re.I),
    re.compile(r"used by (?:the |your )?([a-z0-9'\s,-]+?)(?: this card| do|\.|,|$)", re.I),
    re.compile(r"attach (?:it |them )?to (?:1 of )?(?:your )?([a-z0-9'\s,-]+?)(?:\.|$)", re.I),
    re.compile(r"attached to (?:the |your )?([a-z0-9'\s,-]+?)(?: this card| takes|\.|,|$)", re.I),
    re.compile(r"to (?:your )?([a-z0-9'\s,-]+?) in the active spot", re.I),
)


def _trainer_named_targets(
    trainer: CardRecord,
    pk_vocab: set[str],
) -> tuple[set[str], bool]:
    """Extrae nombres de Pokémon específicos exigidos o potenciados por un Entrenador."""
    txt = trainer.card_text
    if not txt:
        return set(), False
    low = txt.lower()
    if "the basic pokémon" in low:
        return set(), False

    matched: set[str] = set()
    strict_clause = False
    vocab_sorted = sorted((n for n in pk_vocab if len(n) >= 3), key=len, reverse=True)
    for pat in _TRAINER_TARGET_PATTERNS:
        m = pat.search(txt)
        if not m:
            continue
        seg = m.group(1).lower()
        found_here = [n for n in vocab_sorted if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", seg)]
        if found_here:
            matched.update(found_here)
            strict_clause = True
    return matched, strict_clause


def _find_synergy_trainers_for_pokemon(
    card: CardRecord,
    trainer_pool: list[CardRecord],
    pk_vocab: set[str],
) -> list[CardRecord]:
    """Devuelve Entrenadores con sinergia explícita directa hacia un Pokémon."""
    cname = card.name.lower()
    hits: list[CardRecord] = []
    for tr in trainer_pool:
        targets, _ = _trainer_named_targets(tr, pk_vocab)
        if cname in targets:
            hits.append(tr)
    return hits


def _fill_trainers(
    pokemon_cards: list[CardRecord],
    trainer_pool: list[CardRecord],
    target_total: int = 20,
    prefer_set: str | None = None,
    prioritize_set: bool = False,
) -> list[CardRecord]:
    """Completa hasta 20 cartas eligiendo los Entrenadores con sinergia real al mazo."""
    needed = target_total - len(pokemon_cards)
    if needed <= 0:
        return []

    pk_names = {c.name.lower() for c in pokemon_cards}
    pk_vocab = pk_names | {n for n in pk_names}
    evolved_from_names = {c.evolves_from.lower() for c in pokemon_cards if c.evolves_from}
    basic_frontliners = [
        c for c in pokemon_cards
        if c.stage == "Basic" and c.name.lower() not in evolved_from_names
    ]
    distinct_subtypes = {c.subtype for c in pokemon_cards if c.subtype and c.subtype != "Colorless"}
    has_stage2 = any(c.stage == "Stage 2" for c in pokemon_cards)
    mentions_stadium = any(
        "stadium" in (t or "").lower()
        for c in pokemon_cards
        for t in (c.card_text, c.ability_text, *c.attack_texts)
    )
    mentions_tool = any(
        "tool" in (t or "").lower()
        for c in pokemon_cards
        for t in (c.card_text, c.ability_text, *c.attack_texts)
    )
    has_energy_ramp = any(c.energy_ramp_subtype() for c in pokemon_cards) or any(
        ("produce" in t.card_text.lower() or "attach" in t.card_text.lower())
        and "energy" in t.card_text.lower()
        and (_trainer_named_targets(t, pk_names)[0] & pk_names)
        for t in trainer_pool
    )
    all_cheap = all(
        min((len(cost) for cost, _ in c.attacks if cost), default=1) <= 1
        for c in pokemon_cards
    )
    has_other_gust = any(
        t.name in {"Sabrina", "Cyrus", "Blue's Provocation"}
        and (not _trainer_named_targets(t, pk_vocab)[1] or bool(_trainer_named_targets(t, pk_vocab)[0] & pk_names))
        for t in trainer_pool
    )
    has_bench_coach = any("as long as this pokémon is in play" in c.ability_text.lower() for c in pokemon_cards)
    multi_line_evo = len(pokemon_cards) >= 10

    def trainer_rank(tr: CardRecord) -> tuple[int, int]:
        txt = tr.card_text.lower()
        score = 0
        if tr.name in CORE_TRAINER_PRIORITY:
            score += 105 - CORE_TRAINER_PRIORITY.index(tr.name) * 4
        if tr.name in {"Professor's Research", "Poké Ball"}:
            score += 35

        targets, strict_clause = _trainer_named_targets(tr, pk_vocab)
        if strict_clause:
            if targets & pk_names:
                score += 155
                if "energy" in txt and ("produce" in txt or "attach" in txt or "move" in txt):
                    score += 35
            else:
                return (-900, 0)

        if tr.name == "Rare Candy":
            if not has_stage2:
                return (-900, 0)
            score += 145

        if "3 or more different pokémon types" in txt and len(distinct_subtypes) < 3:
            return (-900, 0)

        if "eviolite" in tr.name.lower() or "the basic pokémon this card is attached to" in txt:
            if not basic_frontliners:
                return (-450, 0)
            score += 72 if len(basic_frontliners) >= 2 else 38

        if tr.subtype == "Stadium":
            if mentions_stadium:
                score += 125
            elif "draw a card" in txt and not multi_line_evo:
                score += 78
            else:
                score += 30

        if "tool" in tr.subtype.lower() and mentions_tool and tr.name != "Eviolite":
            score += 55

        if "retreat cost" in txt or tr.name == "X Speed":
            if has_bench_coach or multi_line_evo:
                score += 65

        if "max repel" in tr.name.lower():
            if has_other_gust or has_bench_coach:
                score += 20
            else:
                score += 82

        if tr.name == "Poké Mart Guy" or ("super potion" in txt and "max repel" in txt):
            score -= 180

        if ("discard" in txt and "energy" in txt) or tr.name == "Super Potion":
            if has_energy_ramp and all_cheap:
                score -= 25
            else:
                score -= 220

        if prefer_set and tr.set_code == prefer_set:
            score += 130 if prioritize_set else 20
        return (score, 1 if tr.subtype == "Supporter" else 0)

    ranked = sorted(trainer_pool, key=trainer_rank, reverse=True)
    chosen: list[CardRecord] = []
    used_names: set[str] = set()
    for tr in ranked:
        r_score, _ = trainer_rank(tr)
        if r_score <= -800:
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


def _find_tribal_attack_partners(
    anchor: CardRecord,
    pokemon_pool: list[CardRecord],
) -> list[CardRecord]:
    """Resuelve la clausura transitiva de una tribu por ataque amplificado (Coach + Strikers)."""
    all_attack_names = {
        an.lower()
        for c in pokemon_pool
        for an in c.attack_names
        if len(an) >= 4
    }

    # Identifica ataques que algún Pokémon del pool amplifica desde su habilidad
    coached_attacks: dict[str, list[CardRecord]] = {}
    for c in pokemon_pool:
        ab_low = c.ability_text.lower()
        if not ab_low:
            continue
        for atk_name in all_attack_names:
            if atk_name in ab_low:
                coached_attacks.setdefault(atk_name, []).append(c)

    if not coached_attacks:
        return []

    anchor_atks = {n.lower() for n in anchor.attack_names if len(n) >= 4}
    anchor_ab = anchor.ability_text.lower()
    matched_attacks = {
        atk_name
        for atk_name in coached_attacks
        if atk_name in anchor_atks or atk_name in anchor_ab
    }
    if not matched_attacks:
        return []

    raw_members = [
        c for c in pokemon_pool
        if any(a in c.ability_text.lower() for a in matched_attacks)
        or ({n.lower() for n in c.attack_names if len(n) >= 4} & matched_attacks)
    ]
    # Solo descarta pre-evoluciones cuyo siguiente eslabón también pertenece a la misma tribu
    tribal_evolved_from = {c.evolves_from.lower() for c in raw_members if c.evolves_from}

    coaches: list[CardRecord] = []
    strikers: list[CardRecord] = []
    for cand in raw_members:
        if cand.name == anchor.name:
            continue
        if cand.name.lower() in tribal_evolved_from:
            continue
        c_ab = cand.ability_text.lower()
        if any(a in c_ab for a in matched_attacks):
            coaches.append(cand)
        else:
            strikers.append(cand)

    strikers.sort(key=lambda c: (c.subtype == "Colorless", c.score()), reverse=True)
    return coaches + strikers


def _tribal_combo_ev_bonus(
    card: CardRecord,
    pokemon_pool: list[CardRecord],
) -> float:
    """Calcula el bono matemático de valor esperado (EV) cuando existe un Coach de monedas/daño."""
    partners = _find_tribal_attack_partners(card, pokemon_pool)
    if not partners:
        return 0.0

    members = [card, *partners]
    tribal_evolved_from = {c.evolves_from.lower() for c in members if c.evolves_from}
    if card.name.lower() in tribal_evolved_from:
        return 0.0

    extra_coins_per_coach = 0
    for m in members:
        m_c = re.search(r"flip\s+(\d+)\s+more\s+coins", m.ability_text, re.I)
        if m_c:
            extra_coins_per_coach = max(extra_coins_per_coach, int(m_c.group(1)))

    if extra_coins_per_coach <= 0:
        return 45.0

    # Con 2 copias del Coach en el mazo (~1.5 activas en mesa), evalúa EV/energía de los atacantes
    best_ev_per_energy = 0.0
    striker_lines = 0
    for m in members:
        for (cost, dmg), atk_txt in zip(m.attacks, m.attack_texts):
            m_flip = re.search(r"flip\s+(\d+)\s+coins?", atk_txt, re.I)
            if m_flip and dmg > 0:
                striker_lines += 1
                total_coins = int(m_flip.group(1)) + (1.5 * extra_coins_per_coach)
                ev_dmg = total_coins * 0.5 * dmg
                ev_eff = ev_dmg / max(len(cost), 1)
                best_ev_per_energy = max(best_ev_per_energy, ev_eff)

    return min(145.0, (best_ev_per_energy * 0.78) + (striker_lines * 16.0))


def _candidate_synergy_score(
    card: CardRecord,
    *,
    pokemon_pool: list[CardRecord],
    trainer_pool: list[CardRecord],
    pk_vocab: set[str],
    has_rare_candy: bool,
) -> float:
    """Evalúa valor competitivo real combinando EV, rampa de energía, tempo y entrenadores."""
    base = card.score()
    min_cost = min((len(cost) for cost, _ in card.attacks if cost), default=3)
    if card.stage == "Stage 2" and not has_rare_candy:
        base -= 26.0
        if min_cost >= 4:
            base -= 42.0

    # Daño condicional ("does X more damage") y presión temprana de 1 energía
    best_eff_dmg = max((dmg for _, dmg in card.attacks), default=0)
    for (cost, dmg), atk_txt in zip(card.attacks, card.attack_texts):
        low_eff = atk_txt.lower()
        m_more = re.search(r"does\s+(\d+)\s+more\s+damage", low_eff)
        if m_more and "at least" not in low_eff and "both of them" not in low_eff:
            cond_dmg = int(m_more.group(1))
            best_eff_dmg = max(best_eff_dmg, dmg + int(cond_dmg * 0.70))
        if "damage to each of your benched pokémon" in low_eff:
            base -= 18.0
        if "discard the top" in low_eff and "of your deck" in low_eff:
            base -= 12.0

    if card.stage in {"Basic", "Stage 1"} and min_cost == 1 and best_eff_dmg >= 60:
        base += 26.0

    ramp_types = {
        r for c in pokemon_pool if c.stage in {"Basic", "Stage 1"} or has_rare_candy
        for r in [c.energy_ramp_subtype()] if r
    }
    synergy_trs = _find_synergy_trainers_for_pokemon(card, trainer_pool, pk_vocab)
    trainer_ramps = False
    for tr in synergy_trs:
        tl = tr.card_text.lower()
        if ("produce" in tl or "attach" in tl) and "energy" in tl:
            base += 42.0
            trainer_ramps = True
        elif "switch in" in tl or "+30 damage" in tl:
            base += 28.0
        else:
            base += 16.0

    if card.energy_discard_severity() > 0 and card.subtype not in ramp_types and not trainer_ramps:
        base -= 58.0

    base += _tribal_combo_ev_bonus(card, pokemon_pool)

    all_txt = " ".join(card.attack_texts).lower()
    if "stadium" in all_txt and any(t.subtype == "Stadium" for t in trainer_pool):
        base += 26.0
    return base


def _assemble_synergy_pokemon(
    *,
    anchor: CardRecord,
    core: list[CardRecord],
    pokemon_pool: list[CardRecord],
    trainer_pool: list[CardRecord],
    require_no_ex: bool,
    require_diamond: bool,
    only_type: str | None,
    effective_set: str | None,
    exclude_names: set[str] | None = None,
) -> list[CardRecord]:
    """Acopla compañeros de sinergia (tribales, rampa o soporte ágil) preservando energía."""
    pokemon = list(core)
    excluded = exclude_names or set()
    max_energies = 2 if anchor.subtype == "Dragon" else 1
    pk_vocab = {c.name.lower() for c in pokemon_pool}

    def can_fit(addition: list[CardRecord], *, max_slots: int = 12) -> bool:
        if not addition or len(pokemon) + len(addition) > max_slots:
            return False
        if any(
            (require_no_ex and (c.is_ex or c.is_mega))
            or (require_diamond and not c.is_diamond_pokemon)
            or (only_type and c.subtype != only_type)
            for c in addition
        ):
            return False
        return _distinct_energy_count(pokemon + addition) <= max_energies

    def already_in(name: str) -> bool:
        return name.lower() in {p.name.lower() for p in pokemon}

    # 1. Sinergia tribal por nombre de ataque compartido (ej. Raboot + Azumarill + Lopunny)
    tribals = _find_tribal_attack_partners(anchor, pokemon_pool)
    for mate in tribals:
        if already_in(mate.name):
            continue
        mate_core = _build_evolution_core(mate, pokemon_pool)
        if can_fit(mate_core, max_slots=12):
            pokemon.extend(mate_core)

    # 2. Compañeros mencionados por los mismos Entrenadores exclusivos (ej. Rika -> Whiscash)
    anchor_trs = _find_synergy_trainers_for_pokemon(anchor, trainer_pool, pk_vocab)
    for tr in anchor_trs:
        targets, _ = _trainer_named_targets(tr, pk_vocab)
        for tname in sorted(targets):
            if already_in(tname):
                continue
            cand = next((c for c in pokemon_pool if c.name.lower() == tname and not already_in(c.name)), None)
            if not cand:
                continue
            cand_core = _build_evolution_core(cand, pokemon_pool)
            # Prueba pareja 2x y si no cabe, 1x por carta de la cadena (ej. 1 Barboach + 1 Whiscash)
            if can_fit(cand_core, max_slots=10):
                pokemon.extend(cand_core)
            else:
                single_chain = list(dict.fromkeys(cand_core))
                if can_fit(single_chain, max_slots=10):
                    pokemon.extend(single_chain)

    # 3. Motor de aceleración de energía si el anchor es pesado o descarta energía
    max_atk_cost = max((len(cost) for cost, _ in anchor.attacks), default=1)
    if len(pokemon) <= 6 and (max_atk_cost >= 3 or anchor.energy_discard_severity() > 0):
        ramps = [
            c for c in pokemon_pool
            if not already_in(c.name)
            and c.energy_ramp_subtype() == anchor.subtype
        ]
        ramps.sort(key=lambda c: (c.stage == "Basic", c.score()), reverse=True)
        for ramp in ramps:
            ramp_core = _build_evolution_core(ramp, pokemon_pool)
            if can_fit(ramp_core, max_slots=10):
                pokemon.extend(ramp_core)
                break

    # 4. Soporte ágil (Básico, Stage 1 o Stage 2 Incoloro que habilite Entrenador exclusivo)
    if len(pokemon) <= 6:
        compatible_subtypes = {anchor.subtype, "Colorless"}
        support_pool = [
            c
            for c in pokemon_pool
            if (
                c.stage in {"Basic", "Stage 1"}
                or (
                    c.stage == "Stage 2"
                    and c.subtype == "Colorless"
                    and len(pokemon) <= 4
                    and bool(_find_synergy_trainers_for_pokemon(c, trainer_pool, pk_vocab))
                )
            )
            and not already_in(c.name)
            and c.name not in excluded
            and c.subtype in compatible_subtypes
            and c.retreat <= 2
            and c.energy_discard_severity() == 0
            and (not require_no_ex or (not c.is_ex and not c.is_mega))
            and (not require_diamond or c.is_diamond_pokemon)
            and (not only_type or c.subtype == only_type)
        ]
        has_candy = any(t.name == "Rare Candy" for t in trainer_pool)
        support_pool.sort(
            key=lambda c: (
                1 if effective_set and c.set_code == effective_set else 0,
                _candidate_synergy_score(
                    c,
                    pokemon_pool=pokemon_pool,
                    trainer_pool=trainer_pool,
                    pk_vocab=pk_vocab,
                    has_rare_candy=has_candy,
                ),
            ),
            reverse=True,
        )
        for sup in support_pool:
            sup_core = _build_evolution_core(sup, pokemon_pool)
            if can_fit(sup_core, max_slots=10):
                pokemon.extend(sup_core)
                break

    return pokemon


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
    """Construye y valida una baraja de 20 cartas alrededor de sinergias reales."""
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
    pk_vocab = {c.name.lower() for c in pokemon_pool}
    has_rare_candy = any(t.name == "Rare Candy" for t in trainer_pool)
    only_type = prefer_subtype if prefer_subtype in constraints.required_types else None

    def _is_fast_attacker(card: CardRecord) -> bool:
        min_cost = min((len(cost) for cost, _ in card.attacks if cost), default=4)
        e_types = {ch for cost, _ in card.attacks for ch in cost if ch in ENERGY_CHAR_TO_ID}
        return (
            card.stage in {"Basic", "Stage 1"}
            and min_cost <= 3
            and len(e_types) <= 1
            and card.energy_discard_severity() == 0
        )

    candidates.sort(
        key=lambda c: (
            (1 if prefer_fast and _is_fast_attacker(c) else 0),
            (1 if min_set and effective_set and c.set_code == effective_set else 0),
            _candidate_synergy_score(
                c,
                pokemon_pool=pokemon_pool,
                trainer_pool=trainer_pool,
                pk_vocab=pk_vocab,
                has_rare_candy=has_rare_candy,
            )
            + (12.0 if not min_set and effective_set and c.set_code == effective_set else 0.0),
        ),
        reverse=True,
    )

    for anchor in candidates[:15]:
        core = _build_evolution_core(anchor, pokemon_pool)
        if not core or len(core) > 10:
            continue

        pokemon = _assemble_synergy_pokemon(
            anchor=anchor,
            core=core,
            pokemon_pool=pokemon_pool,
            trainer_pool=trainer_pool,
            require_no_ex=require_no_ex,
            require_diamond=require_diamond,
            only_type=only_type,
            effective_set=effective_set,
            exclude_names=excluded,
        )

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

    # Baraja 1: cubre misión restrictiva (Sin ex / 1-3◊ / Tipo pedido) o mejor arquetipo rápido
    pref_t1 = constraints.required_types[0] if constraints.required_types else None
    d1 = _build_single_archetype_deck(
        title="Opción 1: Sin Pokémon ex / Rápida" if constraints.no_ex or constraints.diamond_only else "Opción 1: Meta Sinergia",
        mission_tag="Cumple: Sin ex / 1–3◊" if constraints.no_ex or constraints.diamond_only else "Sinergia rápida y consistente",
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
            title="Opción 1: Sin Pokémon ex / Rápida" if constraints.no_ex or constraints.diamond_only else "Opción 1: Meta Sinergia",
            mission_tag="Cumple: Sin ex / 1–3◊" if constraints.no_ex or constraints.diamond_only else "Sinergia rápida y consistente",
            pokemon_pool=pokemon_pool,
            trainer_pool=trainer_pool,
            constraints=constraints,
            prefer_set=target_set,
            require_no_ex=constraints.no_ex,
            require_diamond=constraints.diamond_only,
        )
    if d1:
        decks.append(d1)
        for pk in d1.cards:
            if pk.is_pokemon:
                used_anchors.add(pk.name)
                if pk.subtype != "Colorless":
                    used_types.add(pk.subtype)

    # Baraja 2: cubre Mega ex o segundo tipo elemental fuerte distinto
    alt_types = [
        t for t in ("Fighting", "Psychic", "Darkness", "Water", "Fire", "Grass", "Lightning", "Metal")
        if t not in used_types
    ]
    d2 = None
    for req_mega_flag in ([True, False] if constraints.require_mega_ex else [False]):
        for subtype in (list(constraints.required_types) + alt_types):
            d2 = _build_single_archetype_deck(
                title="Opción 2: Poder Mega / Control" if req_mega_flag else f"Opción 2: Control {SUBTYPE_LABELS_ES.get(subtype, subtype)}",
                mission_tag="Cumple: 1+ Mega ex" if req_mega_flag else "Aceleración y control",
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
        for pk in d2.cards:
            if pk.is_pokemon:
                used_anchors.add(pk.name)
                if pk.subtype != "Colorless":
                    used_types.add(pk.subtype)

    # Baraja 3: temática de la expansión del evento (>=12 cartas si aplica) o tercer arquetipo
    remaining_types = [t for t in alt_types if t not in used_types] + alt_types
    d3 = None
    min_set_req = constraints.min_set_cards
    set_label = min_set_req[0].upper() if min_set_req else target_set.upper()
    tag_3 = (
        f"Temática {set_label} (≥{min_set_req[1]} cartas)"
        if min_set_req
        else f"Alternativa estratégica {target_set.upper()}"
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


_CPU_DECK_HINTS = (
    "drop event",
    "promo-drop",
    "(advanced)",
    "(expert)",
    "(beginner)",
    "(intermediate)",
    "cpu deck",
    "solo battle opponent",
)


def _resolve_dotgg_card(card_code: str, catalog: CardCatalog) -> CardRecord | None:
    """Mapea códigos DotGG (`C1-023`, `PROMO-005`, `PC-004`) al catálogo local."""
    key = card_code.strip().lower()
    if key in catalog.by_card_id:
        return catalog.by_card_id[key]
    if key.startswith("promo-c-"):
        return catalog.by_card_id.get("pc-" + key.split("-")[-1].zfill(3))
    if key.startswith("promo-b-"):
        return catalog.by_card_id.get("pb-" + key.split("-")[-1].zfill(3))
    if key.startswith("promo-"):
        num = key.split("-")[-1].zfill(3)
        return catalog.by_card_id.get(f"pa-{num}")
    return None


def extract_ptcgpocket_event_decks(
    session: requests.Session,
    *,
    detail_html: str,
    constraints: EventDeckConstraints,
    catalog: CardCatalog,
    limit: int = 3,
) -> list[BuiltDeck]:
    """Extrae mazos curados de eventos (`RootOfEmbeddedDeck` en PTCGPocket.gg) vía DotGG."""
    if not detail_html:
        return []
    soup = BeautifulSoup(detail_html, "html.parser")
    widgets = soup.select(".RootOfEmbeddedDeck[data-deck]")
    if not widgets:
        return []

    found: list[BuiltDeck] = []
    seen_slugs: set[str] = set()
    for div in widgets:
        slug = str(div.get("data-deck") or "").strip()
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        prev_h = div.find_previous(["h1", "h2", "h3", "h4"])
        heading = prev_h.get_text(" ", strip=True) if prev_h else slug
        low_meta = f"{heading} {slug}".lower()
        if any(hint in low_meta for hint in _CPU_DECK_HINTS):
            continue

        try:
            atts = json.loads(str(div.get("data-atts") or "{}"))
            esig = str(atts.get("esig") or "").strip()
            api_url = f"https://api.dotgg.gg/cgfw/getdeck?game=pokepocket&slug={urllib.parse.quote(slug)}"
            if esig:
                api_url += f"&esig={urllib.parse.quote(esig)}"
            resp = session.get(api_url, timeout=REQUEST_TIMEOUT_SEC)
            if not resp.ok or not resp.text.strip():
                continue
            payload_json = resp.json()
        except Exception as exc:
            LOGGER.debug("[DotGG] No se pudo leer mazo %s: %s", slug, exc)
            continue

        raw_deck = payload_json.get("deck") or {}
        if not isinstance(raw_deck, dict) or not raw_deck:
            continue

        cards: list[CardRecord] = []
        missing = False
        for cid, qty_raw in raw_deck.items():
            rec = _resolve_dotgg_card(str(cid), catalog)
            if not rec:
                missing = True
                break
            try:
                qty = int(qty_raw)
            except (TypeError, ValueError):
                qty = 0
            cards.extend([rec] * qty)
        if missing or len(cards) != 20:
            continue

        pks = [c for c in cards if c.is_pokemon]
        trs = [c for c in cards if not c.is_pokemon]
        energies = infer_energy_types(pks)
        ok, reason = validate_deck(
            cards,
            energies,
            constraints,
            require_no_ex=constraints.no_ex,
            require_mega=constraints.require_mega_ex,
            min_set=constraints.min_set_cards,
        )
        if not ok:
            LOGGER.debug("[DotGG] Mazo %s descartado por restricción: %s", slug, reason)
            continue

        clean_name = re.sub(r"\s+Event\s+Deck$", "", heading, flags=re.I).strip() or slug
        idx = len(found) + 1
        found.append(
            BuiltDeck(
                title=f"Opción {idx}: {clean_name}",
                mission_tag=f"Meta Evento ({(constraints.target_expansion or catalog.latest_set).upper()})",
                cards=tuple(cards),
                energy_types=tuple(energies),
                qr_payload=encode_deck_payload([c.qr_id for c in trs], [c.qr_id for c in pks], energies),
            )
        )
        if len(found) >= limit:
            break
    return found
