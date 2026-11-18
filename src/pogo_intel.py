"""Motor integral de inteligencia competitiva para Pokémon GO (PvE Raids, Hundo IVs y PvP GBL)."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

import requests

from src.config import REQUEST_TIMEOUT_SEC, URLS
from src.models import AlertItem

LOGGER = logging.getLogger(__name__)

# Multiplicadores oficiales CPM de Pokémon GO para niveles clave de captura y máximo.
CPM_BY_LEVEL: dict[int, float] = {
    15: 0.51739395,  # Investigación / Misiones
    20: 0.59740000,  # Incursión normal / Huevos / GBL
    25: 0.66793400,  # Incursión potenciada por clima
    30: 0.73170000,  # Salvaje normal máximo
    35: 0.76156384,  # Salvaje potenciado por clima máximo
    40: 0.79030000,  # Máximo tradicional (sin Caramelos XL)
    50: 0.84029999,  # Máximo absoluto con Caramelos XL
}

TYPE_LABELS_ES: dict[str, str] = {
    "bug": "Bicho",
    "dark": "Siniestro",
    "dragon": "Dragón",
    "electric": "Eléctrico",
    "fairy": "Hada",
    "fighting": "Lucha",
    "fire": "Fuego",
    "flying": "Volador",
    "ghost": "Fantasma",
    "grass": "Planta",
    "ground": "Tierra",
    "ice": "Hielo",
    "normal": "Normal",
    "poison": "Veneno",
    "psychic": "Psíquico",
    "rock": "Roca",
    "steel": "Acero",
    "water": "Agua",
}

WEATHER_LABELS_ES: dict[str, str] = {
    "clear": "Despejado",
    "sunny": "Soleado",
    "rainy": "Lluvioso",
    "partly cloudy": "Parc. nublado",
    "cloudy": "Nublado",
    "windy": "Viento",
    "snow": "Nieve",
    "fog": "Niebla",
}

_TYPE_DEFENSE_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "normal": (("fighting",), (), ("ghost",)),
    "fire": (("water", "ground", "rock"), ("fire", "grass", "ice", "bug", "steel", "fairy"), ()),
    "water": (("electric", "grass"), ("fire", "water", "ice", "steel"), ()),
    "electric": (("ground",), ("electric", "flying", "steel"), ()),
    "grass": (("fire", "ice", "poison", "flying", "bug"), ("water", "electric", "grass", "ground"), ()),
    "ice": (("fire", "fighting", "rock", "steel"), ("ice",), ()),
    "fighting": (("flying", "psychic", "fairy"), ("bug", "rock", "dark"), ()),
    "poison": (("ground", "psychic"), ("grass", "fighting", "poison", "bug", "fairy"), ()),
    "ground": (("water", "grass", "ice"), ("poison", "rock"), ("electric",)),
    "flying": (("electric", "ice", "rock"), ("grass", "fighting", "bug"), ("ground",)),
    "psychic": (("bug", "ghost", "dark"), ("fighting", "psychic"), ()),
    "bug": (("fire", "flying", "rock"), ("grass", "fighting", "ground"), ()),
    "rock": (("water", "grass", "fighting", "ground", "steel"), ("normal", "fire", "poison", "flying"), ()),
    "ghost": (("ghost", "dark"), ("poison", "bug"), ("normal", "fighting")),
    "dragon": (("ice", "dragon", "fairy"), ("fire", "water", "electric", "grass"), ()),
    "dark": (("fighting", "bug", "fairy"), ("ghost", "dark"), ("psychic",)),
    "steel": (("fire", "fighting", "ground"), ("normal", "grass", "ice", "flying", "psychic", "bug", "rock", "dragon", "steel", "fairy"), ("poison",)),
    "fairy": (("poison", "steel"), ("fighting", "bug", "dark"), ("dragon",)),
}

_LEAGUE_CONFIG: dict[str, tuple[str, str, str, str]] = {
    "great": ("Liga Súper (1500 PC)", "1500", "great", "https://pvpoke.com/rankings/all/1500/overall/"),
    "ultra": ("Liga Ultra (2500 PC)", "2500", "ultra", "https://pvpoke.com/rankings/all/2500/overall/"),
    "ultra_mega": ("Liga Ultra: Mega Edition", "2500", "ultra_mega", "https://pvpoke.com/rankings/mega/2500/overall/"),
    "master": ("Liga Master (Sin límite)", "10000", "master", "https://pvpoke.com/rankings/all/10000/overall/"),
}


def _norm_key(text: str) -> str:
    """Normaliza identificadores de especies y ataques para cruces sin ambigüedad."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def calc_cp(base_atk: int, base_def: int, base_hp: int, iv_a: int, iv_d: int, iv_h: int, level: int) -> int:
    """Calcula el PC exacto oficial de Pokémon GO para unos IVs y nivel entero."""
    cpm = CPM_BY_LEVEL[level]
    raw = ((base_atk + iv_a) * math.sqrt(base_def + iv_d) * math.sqrt(base_hp + iv_h) * (cpm ** 2)) / 10.0
    return max(10, math.floor(raw))


def type_effectiveness_multiplier(attack_type: str, defender_types: tuple[str, ...]) -> float:
    """Calcula el multiplicador oficial de Pokémon GO (1.6× / 2.56× / 0.625× / 0.39×)."""
    atk = attack_type.lower()
    mult = 1.0
    for dt in defender_types:
        d = dt.lower()
        if not d or d == "none":
            continue
        rules = _TYPE_DEFENSE_RULES.get(d)
        if not rules:
            continue
        weak, resists, immune = rules
        if atk in weak:
            mult *= 1.6
        elif atk in resists:
            mult *= 0.625
        elif atk in immune:
            mult *= 0.390625
    return mult


def compute_weaknesses(defender_types: tuple[str, ...]) -> list[tuple[str, float]]:
    """Devuelve todas las debilidades del jefe (>1.05×) ordenadas de ×2.56 a ×1.6."""
    weak: list[tuple[str, float]] = []
    for atk_type in TYPE_LABELS_ES:
        m = type_effectiveness_multiplier(atk_type, defender_types)
        if m > 1.05:
            weak.append((atk_type, round(m, 2)))
    weak.sort(key=lambda kv: (-kv[1], TYPE_LABELS_ES.get(kv[0], kv[0])))
    return weak


def format_species_es(name: str) -> str:
    """Formatea nombres especiales al estilo natural en español (ej. Mega X, X Oscuro)."""
    if name.endswith(" (Mega)"):
        return f"Mega {name[:-7]}"
    if name.endswith(" (Mega X)"):
        return f"Mega {name[:-9]} X"
    if name.endswith(" (Mega Y)"):
        return f"Mega {name[:-9]} Y"
    if name.endswith(" (Shadow)"):
        return f"{name[:-9]} Oscuro"
    if name.endswith(" (Primal)"):
        return f"{name[:-9]} Primigenio"
    if "(Dawn Wings)" in name:
        return name.replace("(Dawn Wings)", "Alas del Alba")
    if "(Dusk Mane)" in name:
        return name.replace("(Dusk Mane)", "Melena Crepuscular")
    return name


def search_token_for_species(name: str) -> str:
    """Devuelve la raíz limpia de especie compatible con el buscador de Pokémon GO."""
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if base.lower().startswith("mega "):
        base = base[5:].strip()
    elif base.lower().startswith("shadow "):
        base = base[7:].strip()
    return base


@dataclass(frozen=True)
class PogoPokemon:
    """Ficha técnica oficial de una especie/forma desde PvPoke GameMaster."""

    dex: int
    species_id: str
    species_name: str
    types: tuple[str, ...]
    atk: int
    defense: int
    hp: int
    fast_moves: tuple[str, ...]
    charged_moves: tuple[str, ...]
    tags: tuple[str, ...]
    default_ivs: dict[str, tuple[float, int, int, int]]
    released: bool
    family_parent: str | None
    evolutions: tuple[str, ...]

    @property
    def is_mega(self) -> bool:
        return "mega" in self.tags or self.species_id.endswith("_mega")

    @property
    def is_shadow(self) -> bool:
        return "shadow" in self.tags or self.species_id.endswith("_shadow")

    def cp_100(self, level: int) -> int:
        return calc_cp(self.atk, self.defense, self.hp, 15, 15, 15, level)


@dataclass(frozen=True)
class PogoMoveStats:
    """Estadísticas PvP/PvE de un movimiento para ranking y cálculo de counters."""

    move_id: str
    name: str
    move_type: str
    pvp_power: float
    pvp_energy: int
    pvp_energy_gain: int
    pvp_turns: int
    pve_dps: float


@dataclass(frozen=True)
class PogoRankedMon:
    """Posición real de un Pokémon en el ranking oficial de PvPoke."""

    rank: int
    species_id: str
    species_name: str
    score: float
    moveset: tuple[str, ...]
    top_counters: tuple[str, ...]


@dataclass(frozen=True)
class PogoRaidBoss:
    """Jefe de incursión activo desde ScrapedDuck/raids.json."""

    name: str
    tier: str
    can_be_shiny: bool
    types: tuple[str, ...]
    weather: tuple[str, ...]
    cp_normal_max: int | None
    cp_boosted_max: int | None


@dataclass(frozen=True)
class PogoTacticalCard:
    """Tarjeta táctica integral Pokémon GO lista para renderizar (1080×720) y enviar."""

    title: str
    subtitle: str
    primary_type: str
    badge_label: str
    left_heading: str
    left_rows: tuple[str, ...]
    right_heading: str
    right_rows: tuple[str, ...]
    explanation: str
    search_label: str
    search_string: str
    info_label: str
    info_url: str
    context_label: str


class PogoCatalog:
    """Índice unificado de especies, movimientos, rankings GBL y raids activos."""

    def __init__(
        self,
        raw_pokemon: list[dict[str, Any]],
        raw_moves: list[dict[str, Any]],
        rankings_by_league: dict[str, list[dict[str, Any]]] | None = None,
        raw_raids: list[dict[str, Any]] | None = None,
        pve_fast: list[dict[str, Any]] | None = None,
        pve_charged: list[dict[str, Any]] | None = None,
    ) -> None:
        self.by_id: dict[str, PogoPokemon] = {}
        self.by_name_lower: dict[str, PogoPokemon] = {}
        self.by_norm: dict[str, PogoPokemon] = {}
        self.all_pokemon: list[PogoPokemon] = []

        for item in raw_pokemon:
            sid = str(item.get("speciesId") or "").strip()
            sname = str(item.get("speciesName") or "").strip()
            stats = item.get("baseStats") or {}
            if not sid or not sname or not stats:
                continue
            types = tuple(t.lower() for t in (item.get("types") or []) if t and t.lower() != "none")
            raw_ivs = item.get("defaultIVs") or {}
            parsed_ivs: dict[str, tuple[float, int, int, int]] = {}
            for lk, vals in raw_ivs.items():
                if isinstance(vals, list) and len(vals) == 4:
                    parsed_ivs[lk] = (float(vals[0]), int(vals[1]), int(vals[2]), int(vals[3]))
            family = item.get("family") or {}
            mon = PogoPokemon(
                dex=int(item.get("dex") or 0),
                species_id=sid,
                species_name=sname,
                types=types or ("normal",),
                atk=int(stats.get("atk") or 100),
                defense=int(stats.get("def") or 100),
                hp=int(stats.get("hp") or 100),
                fast_moves=tuple(item.get("fastMoves") or ()),
                charged_moves=tuple(item.get("chargedMoves") or ()),
                tags=tuple(t.lower() for t in (item.get("tags") or ()) if t),
                default_ivs=parsed_ivs,
                released=bool(item.get("released", False)),
                family_parent=family.get("parent"),
                evolutions=tuple(family.get("evolutions") or ()),
            )
            self.by_id[sid] = mon
            self.by_name_lower.setdefault(sname.lower(), mon)
            self.by_norm.setdefault(_norm_key(sname), mon)
            self.all_pokemon.append(mon)

        pve_lookup: dict[str, float] = {}
        for mv in (pve_fast or []) + (pve_charged or []):
            nm = str(mv.get("name") or "")
            dur_ms = max(float(mv.get("duration") or 1000), 400.0)
            pwr = float(mv.get("power") or 0)
            e_delta = float(mv.get("energy_delta") or 0)
            if nm:
                # Ponderación PvE realista: DPS bruto + bono por ataques cargados multi-barra
                base_dps = pwr / (dur_ms / 1000.0)
                if e_delta < 0:
                    cycle = (100.0 / max(abs(e_delta), 33.0)) ** 0.25
                    pve_lookup[_norm_key(nm)] = base_dps * cycle
                else:
                    eps = max(e_delta, 1.0) / (dur_ms / 1000.0)
                    pve_lookup[_norm_key(nm)] = base_dps + (eps * 0.75)

        self.moves: dict[str, PogoMoveStats] = {}
        for mv in raw_moves:
            mid = str(mv.get("moveId") or "").strip()
            mname = str(mv.get("name") or mid).strip()
            if not mid:
                continue
            turns = max(int(mv.get("turns") or 1), 1)
            pwr = float(mv.get("power") or 0)
            base_lookup_name = re.sub(r"\s*\([^)]*\)", "", mname)
            pve_dps = pve_lookup.get(_norm_key(mname)) or pve_lookup.get(_norm_key(base_lookup_name))
            if pve_dps is None:
                pve_dps = (pwr / (turns * 0.5)) * 1.15 if int(mv.get("energyGain") or 0) > 0 else (pwr * 0.45)
            self.moves[mid] = PogoMoveStats(
                move_id=mid,
                name=mname,
                move_type=str(mv.get("type") or "normal").lower(),
                pvp_power=pwr,
                pvp_energy=int(mv.get("energy") or 0),
                pvp_energy_gain=int(mv.get("energyGain") or 0),
                pvp_turns=turns,
                pve_dps=float(pve_dps),
            )

        self.rankings: dict[str, list[PogoRankedMon]] = {}
        self.rank_lookup: dict[str, dict[str, PogoRankedMon]] = {}
        for lkey, rows in (rankings_by_league or {}).items():
            parsed: list[PogoRankedMon] = []
            by_sid: dict[str, PogoRankedMon] = {}
            for idx, r in enumerate(rows, 1):
                sid = str(r.get("speciesId") or "").strip()
                if not sid:
                    continue
                opps = tuple(str(c.get("opponent") or "") for c in (r.get("counters") or [])[:3] if c.get("opponent"))
                rmon = PogoRankedMon(
                    rank=idx,
                    species_id=sid,
                    species_name=str(r.get("speciesName") or sid),
                    score=float(r.get("score") or 0.0),
                    moveset=tuple(r.get("moveset") or ()),
                    top_counters=opps,
                )
                parsed.append(rmon)
                by_sid.setdefault(sid, rmon)
            self.rankings[lkey] = parsed
            self.rank_lookup[lkey] = by_sid

        self.raids: list[PogoRaidBoss] = []
        for rd in raw_raids or []:
            nm = str(rd.get("name") or "").strip()
            if not nm:
                continue
            cp = rd.get("combatPower") or {}
            norm_max = (cp.get("normal") or {}).get("max")
            boost_max = (cp.get("boosted") or {}).get("max")
            self.raids.append(
                PogoRaidBoss(
                    name=nm,
                    tier=str(rd.get("tier") or ""),
                    can_be_shiny=bool(rd.get("canBeShiny", False)),
                    types=tuple(str(t.get("name") or "").lower() for t in (rd.get("types") or []) if t.get("name")),
                    weather=tuple(str(w.get("name") or "").lower() for w in (rd.get("boostedWeather") or []) if w.get("name")),
                    cp_normal_max=int(norm_max) if norm_max else None,
                    cp_boosted_max=int(boost_max) if boost_max else None,
                )
            )

    def resolve_pokemon(self, query: str) -> PogoPokemon | None:
        """Resuelve un nombre de Pokémon en varios formatos (Mega X, Shadow X, Forma)."""
        clean = " ".join(query.strip().split())
        if not clean:
            return None
        lower = clean.lower()
        if lower in self.by_name_lower:
            return self.by_name_lower[lower]
        if lower.startswith("mega "):
            rest = clean[5:].strip()
            cand = self.by_name_lower.get(f"{rest.lower()} (mega)") or self.by_id.get(f"{_norm_key(rest)}_mega")
            if cand:
                return cand
        if lower.startswith("shadow "):
            rest = clean[7:].strip()
            cand = self.by_name_lower.get(f"{rest.lower()} (shadow)") or self.resolve_pokemon(rest)
            if cand:
                return cand
        if lower.startswith("eternamax "):
            rest = clean[10:].strip()
            cand = self.by_id.get(f"{_norm_key(rest)}_eternamax")
            if cand:
                return cand
        if lower in self.by_id:
            return self.by_id[lower]
        norm = _norm_key(clean)
        if norm in self.by_norm:
            return self.by_norm[norm]
        # Tolerancia cuando el evento omite "(Forme)" o agrega "Forme"
        stripped = _norm_key(re.sub(r"\bforme?\b", "", lower))
        if stripped in self.by_norm:
            return self.by_norm[stripped]
        for cand_norm, mon in self.by_norm.items():
            if stripped and cand_norm.startswith(stripped) and not mon.is_mega and not mon.is_shadow:
                return mon
        return None

    def resolve_catch_form(self, boss: PogoPokemon) -> PogoPokemon:
        """Para Megaevoluciones o Eternamax devuelve la forma base capturable en raids."""
        if boss.is_mega or boss.species_id.endswith("_eternamax") or boss.is_shadow:
            base_id = re.sub(r"_(mega(?:_[xy])?|eternamax|shadow|primal)$", "", boss.species_id)
            if base_id in self.by_id:
                return self.by_id[base_id]
        return boss

    def move_label(self, move_id: str) -> str:
        mv = self.moves.get(move_id)
        return mv.name if mv else move_id.replace("_", " ").title()

    def best_pvp_entry(self, species_id: str) -> tuple[str, PogoRankedMon] | None:
        """Busca la mejor liga donde compite una especie dentro de PvPoke."""
        best: tuple[float, str, PogoRankedMon] | None = None
        for lkey in ("great", "ultra", "master"):
            entry = self.rank_lookup.get(lkey, {}).get(species_id)
            if entry and entry.rank <= 250:
                score_val = entry.score - (entry.rank * 0.05)
                if best is None or score_val > best[0]:
                    best = (score_val, lkey, entry)
        return (best[1], best[2]) if best else None


def _safe_fetch_json(session: requests.Session, url: str) -> Any:
    resp = session.get(url, timeout=max(REQUEST_TIMEOUT_SEC, 20))
    resp.raise_for_status()
    return resp.json()


def load_pogo_catalog(session: requests.Session) -> PogoCatalog:
    """Descarga el catálogo integral $0 de PvPoke, ScrapedDuck Raids y PvE moves."""
    raw_pk = _safe_fetch_json(session, URLS["pvpoke_gamemaster_pokemon"])
    raw_mv = _safe_fetch_json(session, URLS["pvpoke_gamemaster_moves"])
    rankings: dict[str, list[dict[str, Any]]] = {}
    for lkey, ukey in (
        ("great", "pvpoke_rankings_1500"),
        ("ultra", "pvpoke_rankings_2500"),
        ("master", "pvpoke_rankings_10000"),
        ("ultra_mega", "pvpoke_rankings_mega_2500"),
    ):
        try:
            rankings[lkey] = _safe_fetch_json(session, URLS[ukey])
        except Exception as exc:
            LOGGER.warning("[POGO] Sin ranking %s (%s).", lkey, exc)

    raw_raids: list[dict[str, Any]] = []
    try:
        raw_raids = _safe_fetch_json(session, URLS["scrapedduck_raids"])
    except Exception as exc:
        LOGGER.warning("[POGO] Sin raids.json (%s).", exc)

    pve_fast: list[dict[str, Any]] = []
    pve_charged: list[dict[str, Any]] = []
    try:
        pve_fast = _safe_fetch_json(session, URLS["pogoapi_fast_moves"])
        pve_charged = _safe_fetch_json(session, URLS["pogoapi_charged_moves"])
    except Exception as exc:
        LOGGER.warning("[POGO] Sin PvE moves de pogoapi (%s); usando respaldo PvPoke.", exc)

    return PogoCatalog(raw_pk, raw_mv, rankings, raw_raids, pve_fast, pve_charged)


def _best_moveset_for_boss(
    attacker: PogoPokemon,
    boss_types: tuple[str, ...],
    catalog: PogoCatalog,
) -> tuple[float, str, str]:
    """Calcula el mejor par Rápido + Cargado contra un jefe de incursión concreto."""
    shadow_atk_mult = 1.20 if attacker.is_shadow else 1.0
    best_fast: tuple[float, PogoMoveStats] | None = None
    best_charged: tuple[float, PogoMoveStats] | None = None

    for mid in attacker.fast_moves:
        mv = catalog.moves.get(mid)
        if not mv or mid.startswith("HIDDEN_POWER_"):
            continue
        stab = 1.2 if mv.move_type in attacker.types else 1.0
        eff = type_effectiveness_multiplier(mv.move_type, boss_types)
        score = mv.pve_dps * stab * eff
        if best_fast is None or score > best_fast[0]:
            best_fast = (score, mv)

    for mid in attacker.charged_moves:
        mv = catalog.moves.get(mid)
        if not mv or mid in ("FRUSTRATION", "RETURN"):
            continue
        stab = 1.2 if mv.move_type in attacker.types else 1.0
        eff = type_effectiveness_multiplier(mv.move_type, boss_types)
        score = mv.pve_dps * stab * eff
        if best_charged is None or score > best_charged[0]:
            best_charged = (score, mv)

    if not best_fast or not best_charged:
        return (0.0, "", "")

    eff_fast = type_effectiveness_multiplier(best_fast[1].move_type, boss_types)
    eff_charged = type_effectiveness_multiplier(best_charged[1].move_type, boss_types)
    if eff_charged <= 1.05 and eff_fast <= 1.05:
        return (0.0, "", "")

    raw_power = (
        (attacker.atk + 15)
        * shadow_atk_mult
        * (best_fast[0] * 0.35 + best_charged[0] * 0.65)
        * ((attacker.defense * attacker.hp) ** 0.08)
    )
    return (raw_power, best_fast[1].name, best_charged[1].name)


def compute_raid_counters(
    boss_types: tuple[str, ...],
    catalog: PogoCatalog,
    *,
    limit: int = 6,
) -> list[tuple[PogoPokemon, str, str]]:
    """Selecciona Top counters diversos (máx. 1 Mega y sin repetir mismo Dex)."""
    scored: list[tuple[float, PogoPokemon, str, str]] = []
    for mon in catalog.all_pokemon:
        if not mon.released or "supermega" in mon.tags:
            continue
        pwr, f_name, c_name = _best_moveset_for_boss(mon, boss_types, catalog)
        if pwr > 0:
            scored.append((pwr, mon, f_name, c_name))

    scored.sort(key=lambda row: -row[0])
    chosen: list[tuple[PogoPokemon, str, str]] = []
    seen_dex: set[int] = set()
    mega_used = 0
    for _, mon, f_name, c_name in scored:
        if mon.dex in seen_dex:
            continue
        if mon.is_mega:
            if mega_used >= 1:
                continue
            mega_used += 1
        seen_dex.add(mon.dex)
        chosen.append((mon, f_name, c_name))
        if len(chosen) >= limit:
            break
    return chosen


def _find_matching_raid_entry(boss_name: str, catalog: PogoCatalog) -> PogoRaidBoss | None:
    target = catalog.resolve_pokemon(boss_name)
    low = boss_name.lower().strip()
    for rd in catalog.raids:
        if target and catalog.resolve_pokemon(rd.name) == target:
            return rd
        if rd.name.lower() == low or low in rd.name.lower() or rd.name.lower() in low:
            return rd
    return None


def build_raid_card(
    boss_name: str,
    catalog: PogoCatalog,
    *,
    context_title: str,
    badge_label: str = "GUÍA RAID GO",
) -> PogoTacticalCard | None:
    """Construye la tarjeta táctica de Raid (PC 100% IV, debilidades, counters y filtro)."""
    boss = catalog.resolve_pokemon(boss_name)
    raid_entry = _find_matching_raid_entry(boss_name, catalog)
    if not boss:
        return None

    catch_mon = catalog.resolve_catch_form(boss)
    cp_20 = raid_entry.cp_normal_max if (raid_entry and raid_entry.cp_normal_max) else catch_mon.cp_100(20)
    cp_25 = raid_entry.cp_boosted_max if (raid_entry and raid_entry.cp_boosted_max) else catch_mon.cp_100(25)

    types_es = "/".join(TYPE_LABELS_ES.get(t, t.title()) for t in boss.types)
    weak = compute_weaknesses(boss.types)
    weak_lines = [
        f"• {TYPE_LABELS_ES.get(t, t.title())} (×{mult:.2g})"
        for t, mult in weak[:5]
    ]

    weather_txt = (
        ", ".join(WEATHER_LABELS_ES.get(w, w.title()) for w in raid_entry.weather)
        if raid_entry and raid_entry.weather
        else "Según tipo"
    )
    shiny_txt = " • Variocolor disponible" if (raid_entry and raid_entry.can_be_shiny) else ""
    tier_map = {
        "5-star raids": "Raid Nivel 5",
        "3-star raids": "Raid Nivel 3",
        "1-star raids": "Raid Nivel 1",
        "mega raids": "Mega Raid",
    }
    tier_clean = tier_map.get(raid_entry.tier.lower(), raid_entry.tier) if (raid_entry and raid_entry.tier) else ""
    tier_txt = f" • {tier_clean}" if tier_clean else ""

    left_rows = [
        f"Tipos: {types_es}",
        f"PC 100% Nv.20: {cp_20}",
        f"PC 100% Clima Nv.25: {cp_25}",
        f"Clima: {weather_txt}",
        "— DEBILIDADES CLAVE —",
        *weak_lines[:4],
    ]

    counters = compute_raid_counters(boss.types, catalog, limit=6)
    right_rows = [
        f"{idx}. {format_species_es(mon.species_name)} ({f_mv} + {c_mv})"
        for idx, (mon, f_mv, c_mv) in enumerate(counters, 1)
    ]

    search_terms: list[str] = []
    for mon, _, _ in counters:
        tok = search_token_for_species(mon.species_name)
        if tok and tok not in search_terms:
            search_terms.append(tok)
    search_string = ",".join(search_terms[:6])

    top_weak_es = ", ".join(TYPE_LABELS_ES.get(t, t.title()) for t, _ in weak[:3])
    top_counter_es = format_species_es(counters[0][0].species_name) if counters else "counters súper eficaces"
    boss_es = format_species_es(boss.species_name)

    return PogoTacticalCard(
        title=f"{boss_es}{tier_txt}",
        subtitle=f"PC 100%: {cp_20} (Nv.20) • {cp_25} (Nv.25){shiny_txt}",
        primary_type=boss.types[0],
        badge_label=badge_label,
        left_heading="DATOS Y DEBILIDADES",
        left_rows=tuple(left_rows),
        right_heading="TOP 6 COUNTERS RAID",
        right_rows=tuple(right_rows or ["Sin counters calculables."]),
        explanation=(
            f"Vulnerable a {top_weak_es}. Lidera tu equipo con {top_counter_es} "
            f"y verifica el 100% IV al capturar: {cp_20} (normal) o {cp_25} (potenciado)."
        ),
        search_label="Filtro de búsqueda Counters (toca para copiar):",
        search_string=search_string or boss_es,
        info_label="🔎 Ver detalle del evento",
        info_url="",
        context_label=f"Evento GO: {context_title}",
    )


def _family_chain(base_mon: PogoPokemon, catalog: PogoCatalog) -> list[PogoPokemon]:
    """Recorre una familia evolutiva completa de menor a mayor etapa."""
    chain: list[PogoPokemon] = [base_mon]
    queue = list(base_mon.evolutions)
    seen = {base_mon.species_id}
    while queue:
        nxt_id = queue.pop(0)
        if nxt_id in seen:
            continue
        nxt = catalog.by_id.get(nxt_id)
        if not nxt or nxt.is_mega or nxt.is_shadow:
            continue
        seen.add(nxt_id)
        chain.append(nxt)
        queue.extend(nxt.evolutions)
    return chain


def build_feature_pogo_card(
    species_name: str,
    catalog: PogoCatalog,
    *,
    context_title: str,
    badge_label: str = "HUNDO & PvP GO",
) -> PogoTacticalCard | None:
    """Construye guía de PC 100% IV, evolución, veredicto PvP y filtro de caja."""
    base_mon = catalog.resolve_pokemon(species_name)
    if not base_mon:
        return None
    chain = _family_chain(base_mon, catalog)
    final_mon = chain[-1]
    chain_names = " → ".join(mon.species_name for mon in chain[:3])

    left_rows = [
        f"Base: {base_mon.species_name} (PC 100%)",
        f"• Salvaje máx Nv.30: {base_mon.cp_100(30)}",
        f"• Clima máx Nv.35:  {base_mon.cp_100(35)}",
        f"• Misión / Huevo:   {base_mon.cp_100(15)} / {base_mon.cp_100(20)}",
    ]
    if final_mon.species_id != base_mon.species_id:
        left_rows.extend(
            [
                f"Final: {final_mon.species_name}",
                f"• PC 100% Nv.40/50: {final_mon.cp_100(40)} / {final_mon.cp_100(50)}",
            ]
        )

    right_rows: list[str] = []
    best_pvp_note = ""
    for mon in chain:
        hit = catalog.best_pvp_entry(mon.species_id)
        if hit:
            lkey, rmon = hit
            l_label = {"great": "Súper 1500", "ultra": "Ultra 2500", "master": "Master"}[lkey]
            moves_txt = " / ".join(catalog.move_label(m) for m in rmon.moveset[:3])
            iv_key = {"great": "cp1500", "ultra": "cp2500"}.get(lkey)
            iv_vals = mon.default_ivs.get(iv_key) if iv_key else None
            iv_txt = f" • IV {iv_vals[1]}/{iv_vals[2]}/{iv_vals[3]}" if iv_vals else ""
            right_rows.append(f"{mon.species_name} #{rmon.rank} {l_label} ({rmon.score:.1f}){iv_txt}")
            right_rows.append(f"  -> {moves_txt}")
            if not best_pvp_note:
                best_pvp_note = f"{mon.species_name} rinde #{rmon.rank} en {l_label}"

    # Evaluar utilidad PvE del atacante final priorizando movimientos con STAB real
    pve_note = ""
    best_mv = ""
    if final_mon.fast_moves and final_mon.charged_moves:
        f_top = max(
            (catalog.moves[m] for m in final_mon.fast_moves if m in catalog.moves),
            key=lambda m: m.pve_dps * (1.35 if m.move_type in final_mon.types else 1.0),
            default=None,
        )
        c_top = max(
            (catalog.moves[m] for m in final_mon.charged_moves if m in catalog.moves and m not in ("FRUSTRATION", "RETURN")),
            key=lambda m: m.pve_dps * (1.35 if m.move_type in final_mon.types else 1.0),
            default=None,
        )
        if f_top and c_top:
            best_mv = f"{f_top.name} + {c_top.name}"
            right_rows.append(f"PvE Raids ({TYPE_LABELS_ES.get(final_mon.types[0], final_mon.types[0])}): {best_mv}")
            pve_note = f"para incursiones con {best_mv}"

    if not right_rows:
        right_rows.append("Uso principal: colección / caramelos")

    pvp_summary = best_pvp_note or f"enfocado en caramelos y {pve_note or 'colección'}"
    dex_filter = f"+{search_token_for_species(base_mon.species_name)}&4*,3*"
    types_es = "/".join(TYPE_LABELS_ES.get(t, t.title()) for t in final_mon.types)

    return PogoTacticalCard(
        title=chain_names,
        subtitle=f"Tipos: {types_es} • Salvaje Hundo Nv.30/35: {base_mon.cp_100(30)} / {base_mon.cp_100(35)}",
        primary_type=final_mon.types[0],
        badge_label=badge_label,
        left_heading="TABLA PC 100% (HUNDO)",
        left_rows=tuple(left_rows),
        right_heading="VEREDICTO PvP Y RAIDS",
        right_rows=tuple(right_rows[:7]),
        explanation=(
            f"Familia {chain_names}: revisa {base_mon.cp_100(30)} (Nv.30) y {base_mon.cp_100(35)} (clima) para 100% IV; "
            f"en competitivo {pvp_summary}."
        ),
        search_label="Filtro de caja (familia + 3★/4★, toca para copiar):",
        search_string=dex_filter,
        info_label="📊 Ver ranking en PvPoke",
        info_url=f"https://pvpoke.com/rankings/all/1500/overall/{final_mon.species_id}/",
        context_label=f"Evento GO: {context_title}",
    )


def build_gbl_league_card(
    league_key: str,
    catalog: PogoCatalog,
    *,
    context_title: str,
    badge_label: str = "META LIGA GBL",
) -> PogoTacticalCard | None:
    """Construye tarjeta táctica con Top 6 real de PvPoke, movesets e IVs por liga."""
    cfg = _LEAGUE_CONFIG.get(league_key)
    if not cfg:
        return None
    title, cp_cap, _, pvpoke_url = cfg
    ranked = catalog.rankings.get(league_key) or []
    if not ranked:
        return None

    top1 = ranked[0]
    mon1 = catalog.by_id.get(top1.species_id)
    primary_type = mon1.types[0] if mon1 else "steel"

    left_rows: list[str] = []
    search_tokens: list[str] = []
    for entry in ranked[:6]:
        mon = catalog.by_id.get(entry.species_id)
        iv_txt = ""
        if mon and cp_cap in ("1500", "2500"):
            iv = mon.default_ivs.get(f"cp{cp_cap}")
            if iv:
                iv_txt = f" [{iv[1]}/{iv[2]}/{iv[3]}]"
        left_rows.append(f"#{entry.rank} {format_species_es(entry.species_name)} ({entry.score:.1f}){iv_txt}")
        tok = search_token_for_species(entry.species_name)
        if tok and tok not in search_tokens:
            search_tokens.append(tok)

    right_rows: list[str] = []
    for entry in ranked[:5]:
        mv_txt = " • ".join(catalog.move_label(m) for m in entry.moveset[:3])
        right_rows.append(f"#{entry.rank} {mv_txt}")

    counters1 = ", ".join(
        format_species_es(catalog.by_id[c].species_name) if c in catalog.by_id else c.title()
        for c in top1.top_counters[:3]
    )
    if counters1:
        right_rows.append(f"Frenos al #1: {counters1}")

    return PogoTacticalCard(
        title=title,
        subtitle=f"#1 Actual: {format_species_es(top1.species_name)} • Score {top1.score:.1f} (PvPoke)",
        primary_type=primary_type,
        badge_label=badge_label,
        left_heading="TOP 6 META Y IV IDEAL",
        left_rows=tuple(left_rows),
        right_heading="MOVESET ÓPTIMO Y FRENOS",
        right_rows=tuple(right_rows),
        explanation=(
            f"{title} liderada por {format_species_es(top1.species_name)} "
            f"({' + '.join(catalog.move_label(m) for m in top1.moveset[:3])}); "
            f"vigila counters como {counters1 or 'el núcleo meta'}."
        ),
        search_label="Filtro caja Top Meta GBL (toca para copiar):",
        search_string=",".join(search_tokens[:6]),
        info_label="📊 Ver ranking PvPoke",
        info_url=pvpoke_url,
        context_label=context_title,
    )


def _build_weekly_ultra_master_card(catalog: PogoCatalog) -> PogoTacticalCard | None:
    """Tarjeta #2 semanal combinando líderes de Liga Ultra (2500) y Liga Master."""
    ultra = catalog.rankings.get("ultra") or []
    master = catalog.rankings.get("master") or []
    if not ultra and not master:
        return None

    top_u = ultra[0] if ultra else master[0]
    mon_u = catalog.by_id.get(top_u.species_id)
    left_rows = [
        f"#{e.rank} {format_species_es(e.species_name)} ({e.score:.1f}) • {'/'.join(catalog.move_label(m) for m in e.moveset[:2])}"
        for e in ultra[:5]
    ]
    right_rows = [
        f"#{e.rank} {format_species_es(e.species_name)} ({e.score:.1f}) • {'/'.join(catalog.move_label(m) for m in e.moveset[:2])}"
        for e in master[:5]
    ]
    tokens = []
    for e in ultra[:3] + master[:3]:
        t = search_token_for_species(e.species_name)
        if t and t not in tokens:
            tokens.append(t)

    return PogoTacticalCard(
        title="Liga Ultra (2500) y Liga Master",
        subtitle=f"Líder Ultra: {format_species_es(top_u.species_name)} • Líder Master: {format_species_es(master[0].species_name) if master else 'N/D'}",
        primary_type=mon_u.types[0] if mon_u else "dragon",
        badge_label="META SEMANAL GO",
        left_heading="TOP 5 LIGA ULTRA (2500)",
        left_rows=tuple(left_rows or ["Sin datos Ultra."]),
        right_heading="TOP 5 LIGA MASTER (100%)",
        right_rows=tuple(right_rows or ["Sin datos Master."]),
        explanation=(
            f"Panorama semanal de ligas altas en PvPoke: {format_species_es(top_u.species_name)} marca el ritmo en Ultra "
            f"y {format_species_es(master[0].species_name) if master else 'los legendarios 100%'} dominan Master."
        ),
        search_label="Filtro líderes Ultra/Master (toca para copiar):",
        search_string=",".join(tokens[:6]),
        info_label="📊 Ver Ultra en PvPoke",
        info_url="https://pvpoke.com/rankings/all/2500/overall/",
        context_label="Resumen semanal Pokémon GO • PvPoke",
    )


def _build_weekly_active_raids_card(catalog: PogoCatalog) -> PogoTacticalCard | None:
    """Tarjeta #3 semanal resumiendo jefes 5★ y Mega activos con PC 100% y counters."""
    high_raids = [
        r for r in catalog.raids if any(k in r.tier.lower() for k in ("5-star", "mega", "6-star", "primal"))
    ]
    if not high_raids:
        return None

    left_rows: list[str] = []
    right_rows: list[str] = []
    tokens: list[str] = []
    lead_type = high_raids[0].types[0] if high_raids[0].types else "dark"

    for rd in high_raids[:5]:
        boss = catalog.resolve_pokemon(rd.name)
        boss_types = boss.types if boss else (rd.types or ("normal",))
        shiny_tag = " [Shiny]" if rd.can_be_shiny else ""
        cp_n = rd.cp_normal_max or (catalog.resolve_catch_form(boss).cp_100(20) if boss else "?")
        cp_b = rd.cp_boosted_max or (catalog.resolve_catch_form(boss).cp_100(25) if boss else "?")
        left_rows.append(f"{format_species_es(rd.name)}{shiny_tag}: {cp_n} / {cp_b}")

        ctrs = compute_raid_counters(boss_types, catalog, limit=2)
        ctr_txt = ", ".join(format_species_es(m.species_name) for m, _, _ in ctrs)
        weak_txt = "/".join(TYPE_LABELS_ES.get(t, t.title()) for t, _ in compute_weaknesses(boss_types)[:2])
        right_rows.append(f"{format_species_es(rd.name)} ({weak_txt}): {ctr_txt}")
        for m, _, _ in ctrs:
            tok = search_token_for_species(m.species_name)
            if tok and tok not in tokens:
                tokens.append(tok)

    return PogoTacticalCard(
        title="Jefes de Incursión Activos (5* / Mega)",
        subtitle=f"{len(high_raids)} jefes destacados • PC 100% IV (Normal / Potenciado)",
        primary_type=lead_type,
        badge_label="INCURSIONES ACTIVAS",
        left_heading="JEFES Y PC 100% IV",
        left_rows=tuple(left_rows),
        right_heading="DEBILIDAD Y COUNTERS TOP",
        right_rows=tuple(right_rows),
        explanation=(
            "Jefes principales activos esta semana con su PC 100% IV exacto (Nv.20/25) "
            "y los counters más efectivos para tumbarlos en grupo."
        ),
        search_label="Filtro counters para raids de la semana:",
        search_string=",".join(tokens[:6]),
        info_label="🔎 Ver raids en LeekDuck",
        info_url="https://leekduck.com/boss/",
        context_label="Resumen semanal Pokémon GO • ScrapedDuck + PvPoke",
    )


def build_pogo_weekly_meta_report(catalog: PogoCatalog) -> list[PogoTacticalCard]:
    """Construye las 3 tarjetas semanales de Pokémon GO (Súper, Ultra/Master y Raids 5★/Mega)."""
    cards: list[PogoTacticalCard] = []
    great = build_gbl_league_card(
        "great",
        catalog,
        context_title="Resumen semanal Pokémon GO • PvPoke",
        badge_label="META SEMANAL GO",
    )
    ultra_master = _build_weekly_ultra_master_card(catalog)
    raids_card = _build_weekly_active_raids_card(catalog)
    for c in (great, ultra_master, raids_card):
        if c:
            cards.append(c)
    return cards[:3]


_NOISE_TITLE_WORDS = {
    "hora", "para", "tras", "como", "solo", "este", "esta", "sobre", "entre",
    "todo", "todos", "tour", "luna", "moon", "city", "pass", "star", "wish",
    "wishes", "gold", "blue", "alola", "wild", "area", "noir", "mega", "raid",
    "raids", "days", "live", "with", "from", "into", "news", "cup", "game",
    "los", "las", "con", "del", "san", "via", "new", "november", "december",
    "january", "february", "noviembre", "diciembre", "enero", "febrero",
}


def _extract_names_from_title(title: str, catalog: PogoCatalog) -> list[str]:
    """Identifica especies reales mencionadas en un título de evento o noticia Pokémon GO."""
    clean = re.sub(
        r"\b(raid hour|raid day|community day|classic|spotlight hour|max monday|returns|super mega|mega raids|in 5-star raid battles|in shadow raids|during|and|timed research|supermegaincursiones|megaincursiones|gigamax|dinamax)\b",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    found: list[str] = []
    # Primero formas especiales en inglés o español (ej. "Mega Metagross", "Machamp Gigamax")
    for prefix in ("Mega ", "Shadow ", "Eternamax ", "Gigantamax ", "Dynamax "):
        for m in re.finditer(rf"\b{prefix}[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", title):
            raw_phrase = m.group(0)
            cand = catalog.resolve_pokemon(raw_phrase)
            if not cand and prefix in ("Gigantamax ", "Dynamax "):
                cand = catalog.resolve_pokemon(raw_phrase[len(prefix) :])
            if cand and cand.species_name not in found:
                found.append(cand.species_name)

    for m in re.finditer(r"\b([A-Z][a-z]+)\s+(Gigamax|Dinamax)\b", title):
        cand = catalog.resolve_pokemon(m.group(1))
        if cand and cand.species_name not in found:
            found.append(cand.species_name)

    if "mega" in title.lower() and not found:
        # Caso "Clefable Super Mega Raid Day" o "Día de supermegaincursiones: Clefable"
        for tok in re.findall(r"[A-Za-z][a-z'-]+", clean):
            if tok.lower() in _NOISE_TITLE_WORDS or len(tok) < 4:
                continue
            cand = catalog.resolve_pokemon(f"Mega {tok}")
            if cand and cand.species_name not in found:
                found.append(cand.species_name)

    for tok in re.findall(r"[A-Za-z][a-z'-]+", clean):
        if len(tok) < 4 or tok.lower() in _NOISE_TITLE_WORDS:
            continue
        cand = catalog.resolve_pokemon(tok)
        if cand and not any(tok.lower() in f.lower() for f in found):
            found.append(cand.species_name)
    return found


def _extract_structured_species(item: AlertItem, catalog: PogoCatalog) -> list[str]:
    """Extrae especies limpias desde viñetas estructuradas (spawns/destacados) y prefijos especiales."""
    found: list[str] = []

    def _add(raw_name: str) -> None:
        cleaned = re.sub(r"\s+(wearing|with|in|from|con)\b.*$", "", raw_name, flags=re.IGNORECASE).strip(" .,:;")
        if not cleaned or cleaned.lower() in _NOISE_TITLE_WORDS or len(cleaned) < 4:
            return
        mon = catalog.resolve_pokemon(cleaned)
        if mon and mon.species_name not in found:
            found.append(mon.species_name)

    prefixes = (
        "Featured Pokémon:",
        "Pokémon destacados:",
        "Wild spawns:",
        "Pokémon salvajes:",
        "Shinies available:",
        "Variocolor disponibles:",
    )
    for b in item.guide_bullets:
        for pfx in prefixes:
            if b.startswith(pfx):
                body = b[len(pfx) :]
                for part in body.split(","):
                    _add(part)

    # También captura formas especiales explícitas en el resumen ("return of Eternatus", "Gigantamax Machamp")
    summary_text = item.summary or ""
    for m in re.finditer(r"\b(?:Mega|Shadow|Eternamax|Gigantamax|Dynamax)\s+[A-Z][a-z]+", summary_text):
        _add(m.group(0))
        if not catalog.resolve_pokemon(m.group(0)):
            _add(m.group(0).split(None, 1)[-1])
    for m in re.finditer(r"\b(?:return of|featuring|debut of)\s+([A-Z][a-z]+)\b", summary_text, flags=re.IGNORECASE):
        _add(m.group(1))
    return found


def _wrap_bullet_lines(text: str, *, limit: int = 58) -> list[str]:
    """Divide textos largos en líneas limpias legibles sin cortar palabras."""
    words = " ".join(text.split()).split()
    out: list[str] = []
    cur = ""
    for w in words:
        cand = w if not cur else f"{cur} {w}"
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = w
            if len(out) >= 3:
                break
    if cur and len(out) < 3:
        out.append(cur)
    return out


def build_event_playbook_card(
    item: AlertItem,
    extra_ev: dict[str, Any] | None,
    catalog: PogoCatalog,
    *,
    context_title: str,
    featured_species: list[str] | None = None,
) -> PogoTacticalCard | None:
    """Construye tarjeta táctica accionable para eventos de calendario, pases, bonos o noticias GO."""
    ev_data = extra_ev or {}
    ev_type = str(ev_data.get("eventType") or "").lower()
    lower_title = item.title.lower()
    lower_all = " ".join([item.title, item.summary, item.rewards or "", *item.guide_bullets]).lower()
    species_list = list(featured_species or [])
    tokens = [search_token_for_species(s) for s in species_list if search_token_for_species(s)]
    search_tokens_str = ",".join(dict.fromkeys(tokens))[:90]

    # 1. Raids / Día de Incursiones sin jefe aún confirmado -> muestra rotación 5*/Mega actual + preparación
    if ev_type in ("raid-day", "raid-hour", "raid-battles") or any(
        k in lower_title for k in ("día de incursiones", "raid day", "megaincursiones", "super mega")
    ):
        high_raids = [r for r in catalog.raids if any(t in r.tier.lower() for t in ("5-star", "mega", "6-star"))]
        left_rows = [
            f"{format_species_es(r.name)}: PC 100% {r.cp_normal_max or '?'} / {r.cp_boosted_max or '?'}"
            for r in high_raids[:4]
        ] or [*(item.guide_bullets[:3] or _wrap_bullet_lines(item.summary or item.title))]
        right_rows = [
            "Activa Mega Nv.3 del mismo tipo (+Caramelos XL)",
            "Arma equipo predefinido antes de entrar al lobby",
            "Combina Huevo Suerte si encadenas varias incursiones",
        ]
        return PogoTacticalCard(
            title="Plan Táctico de Incursiones",
            subtitle="Jefes actuales en rotación y checklist previo",
            primary_type="fire",
            badge_label="GUÍA RAIDS GO",
            left_heading="ROTACIÓN ACTUAL Y OBJETIVOS",
            left_rows=tuple(left_rows[:5]),
            right_heading="PREPARACIÓN RECOMENDADA",
            right_rows=tuple(right_rows),
            explanation=(
                "Prepara equipo con ventaja elemental y activa una Mega Evolución del tipo del jefe "
                "para maximizar Caramelos XL tras cada captura."
            ),
            search_label="Filtro rápido de atacantes / megas:",
            search_string=search_tokens_str or "megaevolucion1-3,4*,3*",
            info_label="🔎 Ver jefes en LeekDuck",
            info_url=item.url or "https://leekduck.com/boss/",
            context_label=f"Evento GO: {context_title}",
        )

    # 2. Combates Max (Día Max / Dynamax Trails / Lunes Max sin especie única)
    if ev_type in ("max-battles", "max-mondays") or any(
        k in lower_title for k in ("combates max", "max battle", "dynamax", "gigamax", "gigantamax", "lunes max", "max monday")
    ):
        left_rows: list[str] = []
        for b in item.guide_bullets[:3]:
            left_rows.extend(_wrap_bullet_lines(b, limit=62)[:2])
        if not left_rows:
            left_rows = _wrap_bullet_lines(item.summary or "Evento especial centrado en Nodos Energéticos y Combates Max.")
        right_rows = [
            "Recarga límite diario de Partículas Max en Nodos",
            "Prioriza subir Ataque Max antes que Espíritu/Barrera",
            "Usa equipo con ventaja directa frente al jefe del Nodo",
        ]
        return PogoTacticalCard(
            title="Estrategia Combates Max",
            subtitle="Gestión de Partículas Max y Nodos Energéticos",
            primary_type="dragon",
            badge_label="GUÍA MAX GO",
            left_heading="BONUS Y OBJETIVOS DEL EVENTO",
            left_rows=tuple(left_rows[:5]),
            right_heading="RUTA ÓPTIMA EN NODOS",
            right_rows=tuple(right_rows),
            explanation=(
                "Llega con reserva completa de Partículas Max, cobra las de Nodos Energéticos en cada fase "
                "y filtra tus Dinamax/Gigamax antes de entrar."
            ),
            search_label="Filtro Dinamax / Gigamax en caja:",
            search_string=f"{search_tokens_str}&3*,4*" if search_tokens_str else "dinamax,gigamax",
            info_label="📘 Ver detalle oficial",
            info_url=item.url,
            context_label=f"Evento GO: {context_title}",
        )

    # 3. Día de la Comunidad futuro aún sin especie anunciada
    if ev_type == "community-day" or "día de la comunidad" in lower_title or "días de la comunidad" in lower_title or "community day" in lower_title:
        left_rows = [
            "Ventana principal de capturas (3 horas locales)",
            "Evoluciona hasta 5 h después para Ataque Legacy",
            "Mayor probabilidad de Shiny y Módulos Cebo extendidos",
        ]
        right_rows = [
            "Activa Mega Evolución Nv.3 del tipo del protagonista",
            "Conserva 4* (Hundo) + IV bajos en ataque para PvP",
            "Limpia caja al instante con el filtro inferior",
        ]
        return PogoTacticalCard(
            title="Plan Día de la Comunidad",
            subtitle="Ventana Legacy, Mega Caramelos XL y limpieza rápida",
            primary_type="grass",
            badge_label="PLAN CD GO",
            left_heading="REGLAS CLAVE DEL EVENTO",
            left_rows=tuple(left_rows),
            right_heading="CHECKLIST EN CAJA Y CAPTURA",
            right_rows=tuple(right_rows),
            explanation=(
                "Ten espacio libre en caja, Ultra Balls listas y una Mega Nv.3 activa durante todo el horario "
                "para multiplicar Caramelos XL y no perder el ataque exclusivo al evolucionar."
            ),
            search_label="Filtro de limpieza rápida (no shiny / no 100%):",
            search_string="0*,1*,2*&!shiny&!4*",
            info_label="📘 Ver calendario oficial",
            info_url=item.url,
            context_label=f"Evento GO: {context_title}",
        )

    # 4. Día de Eclosiones
    if "eclosion" in lower_title or "hatch day" in lower_title:
        left_rows = [
            "Huevos de evento (generalmente 2 km) con bonus shiny",
            "Posible 1/2 o 1/4 distancia al incubar en la ventana",
            *(item.guide_bullets[:2]),
        ]
        right_rows = [
            "Vacía huevos antiguos justo antes de la hora de inicio",
            "Usa Superincubadoras solo con el bonus ya activo",
            "Enciende Trozo Estrella antes de abrir lote de 9 huevos",
        ]
        return PogoTacticalCard(
            title="Plan Día de Eclosiones",
            subtitle="Optimización de incubadoras, huevos 2km y Polvo",
            primary_type="fairy",
            badge_label="ECLOSIONES GO",
            left_heading="MECÁNICA DEL EVENTO",
            left_rows=tuple(left_rows[:4]),
            right_heading="ESTRATEGIA DE INCUBACIÓN",
            right_rows=tuple(right_rows),
            explanation=(
                "No pongas huevos en incubadora infinita antes de que comience el horario oficial; "
                "aprovecha los tramos reducidos para rotar el máximo de tandas."
            ),
            search_label="Filtro para revisar recién eclosionados:",
            search_string="eclosionado&3*,4*,shiny",
            info_label="📘 Ver detalle del evento",
            info_url=item.url,
            context_label=f"Evento GO: {context_title}",
        )

    # 5. Eventos generales, Bonificaciones, Pases, Temporadas y Noticias Accionables
    left_rows = []
    if item.rewards and "Consultar detalle" not in item.rewards:
        left_rows.extend(_wrap_bullet_lines(item.rewards, limit=62)[:2])
    for b in item.guide_bullets:
        if len(left_rows) >= 4:
            break
        left_rows.extend(_wrap_bullet_lines(b, limit=62)[:2])
    if not left_rows and item.summary:
        left_rows = _wrap_bullet_lines(item.summary, limit=62)[:3]
    if not left_rows:
        left_rows = [f"Evento activo: {context_title}"]

    right_rows = []
    if "lucky" in lower_all or "suerte" in lower_all or "intercambio" in lower_all:
        right_rows.append("Aprovecha probabilidad aumentada de Intercambio con Suerte")
    if "stardust" in lower_all or "polvo" in lower_all:
        right_rows.append("Activa Trozo Estrella en sesiones de captura/recompensas")
    if "xp" in lower_all or "experiencia" in lower_all or "px" in lower_all:
        right_rows.append("Combina Huevo Suerte al subir amistad o completar misiones")
    if "gift" in lower_all or "regalo" in lower_all:
        right_rows.append("Abre y envía regalos hasta el tope diario del Pase")
    if "incense" in lower_all or "incienso" in lower_all:
        right_rows.append("Usa Incienso Aventura Diaria en movimiento para doble duración")
    if not right_rows:
        right_rows = [
            "Revisa Investigaciones Temporales antes del cierre",
            "Usa bayas Pinia / Frambu en los encuentros prioritarios",
            "Marca el evento en el panel al completar sus premios",
        ]

    if search_tokens_str:
        f_str = f"{search_tokens_str}&3*,4*,shiny"
        f_lbl = "Filtro de especies del evento en tu caja:"
    elif "lucky" in lower_all or "suerte" in lower_all or "intercambio" in lower_all:
        f_str = "intercambiar&!favorito"
        f_lbl = "Filtro rápido de candidatos para intercambio:"
    else:
        f_str = "4*,3*,shiny"
        f_lbl = "Filtro general para valorar capturas del evento:"

    return PogoTacticalCard(
        title="Guía y Bonificaciones del Evento",
        subtitle="Resumen accionable, prioridades y filtro rápido de caja",
        primary_type="electric" if item.kind == "news" else "water",
        badge_label="NOTICIA GO" if item.kind == "news" else "GUÍA EVENTO GO",
        left_heading="CLAVES Y RECOMPENSAS",
        left_rows=tuple(left_rows[:4]),
        right_heading="ACCIÓN RECOMENDADA",
        right_rows=tuple(right_rows[:4]),
        explanation=(
            f"{context_title}: prioriza las bonificaciones activas y filtra tus capturas al instante con el buscador."
        ),
        search_label=f_lbl,
        search_string=f_str,
        info_label="📘 Abrir fuente oficial",
        info_url=item.url,
        context_label=f"{'Noticia' if item.kind == 'news' else 'Evento'} GO: {context_title}",
    )


def build_pogo_event_cards(
    item: AlertItem,
    extra_ev: dict[str, Any] | None,
    catalog: PogoCatalog,
    *,
    context_title: str,
    include_playbook: bool = False,
    allow_news: bool = False,
) -> list[PogoTacticalCard]:
    """Genera hasta 3 tarjetas tácticas para eventos (y noticias accionables en Dashboard) de Pokémon GO."""
    if item.game != "pogo" or item.kind == "code":
        return []
    if item.kind == "news" and not allow_news:
        return []

    ev_data = extra_ev or {}
    ev_type = str(ev_data.get("eventType") or "").lower()
    extra = ev_data.get("extraData") or {}
    lower_title = item.title.lower()

    cards: list[PogoTacticalCard] = []

    # 1. Eventos de Liga Combates GO (GBL) o noticias de campeonatos PvP
    if ev_type == "go-battle-league" or any(
        k in lower_title for k in ("great league", "ultra league", "master league", "battle league", "laic", "campeonato internacional")
    ):
        leagues: list[str] = []
        if "great league" in lower_title or "laic" in lower_title or "campeonato internacional" in lower_title:
            leagues.append("great")
        if "ultra league" in lower_title:
            leagues.append("ultra_mega" if "mega" in lower_title and catalog.rankings.get("ultra_mega") else "ultra")
        if "master league" in lower_title:
            leagues.append("master")
        if not leagues:
            leagues = ["great", "ultra", "master"]
        for lkey in leagues[:3]:
            c = build_gbl_league_card(lkey, catalog, context_title=f"Evento GO: {context_title}")
            if c:
                cards.append(c)
        return cards[:3]

    # 2. Eventos de Incursiones, Hora Legendaria, Días de Incursiones y Combates Max
    boss_names: list[str] = [
        b["name"] for b in ((extra.get("raidbattles") or {}).get("bosses") or []) if b.get("name")
    ]
    is_raid_event = bool(boss_names) or ev_type in ("raid-hour", "raid-day", "raid-battles", "max-battles", "max-mondays") or any(
        k in lower_title
        for k in (
            "raid hour", "raid day", "raid battles", "max battle", "max monday",
            "eternamax", "dynamax", "gigantamax", "gigamax", "super mega", "megaincursiones",
        )
    )
    if is_raid_event:
        if not boss_names:
            boss_names = _extract_names_from_title(item.title, catalog)
        if not boss_names:
            struct_names = _extract_structured_species(item, catalog)
            mega_or_legend = [s for s in struct_names if "(mega)" in s.lower() or "eternatus" in s.lower()]
            boss_names = mega_or_legend or ([] if ev_type == "event" else struct_names[:2])
        for bname in boss_names[:3]:
            c = build_raid_card(bname, catalog, context_title=context_title)
            if c:
                cards.append(c)
        if cards:
            if include_playbook and len(cards) < 3 and (item.guide_bullets or (item.rewards and "Consultar" not in item.rewards)):
                pb = build_event_playbook_card(
                    item,
                    extra_ev,
                    catalog,
                    context_title=context_title,
                    featured_species=_extract_structured_species(item, catalog),
                )
                if pb:
                    cards.append(pb)
            return cards[:3]

    # 3. Día de la Comunidad, Hora Destacada o eventos/noticias con Pokémon protagonistas claros
    feat_names: list[str] = []
    cday = extra.get("communityday") or {}
    if cday:
        feat_names.extend(p["name"] for p in (cday.get("spawns") or []) if p.get("name"))
    spotlight = extra.get("spotlight") or {}
    if spotlight:
        feat_names.extend(p["name"] for p in (spotlight.get("list") or []) if p.get("name"))
        if not feat_names and spotlight.get("name"):
            feat_names.append(str(spotlight["name"]))

    if not feat_names:
        feat_names.extend(_extract_names_from_title(item.title, catalog))
    for s in _extract_structured_species(item, catalog):
        if s not in feat_names:
            feat_names.append(s)

    seen_ids: set[str] = set()
    for fname in feat_names:
        mon = catalog.resolve_pokemon(fname)
        if not mon or mon.species_id in seen_ids:
            continue
        seen_ids.add(mon.species_id)
        if mon.is_mega:
            c = build_raid_card(mon.species_name, catalog, context_title=context_title)
        else:
            c = build_feature_pogo_card(mon.species_name, catalog, context_title=context_title)
        if c:
            cards.append(c)
        if len(cards) >= (2 if include_playbook else 3):
            break

    # 4. Para el Dashboard (include_playbook=True): garantiza guía/checklist accionable en cualquier evento restante
    if include_playbook and (
        not cards
        or (len(cards) < 3 and (item.guide_bullets or (item.rewards and "Consultar" not in item.rewards)))
    ):
        pb = build_event_playbook_card(
            item,
            extra_ev,
            catalog,
            context_title=context_title,
            featured_species=feat_names,
        )
        if pb:
            cards.append(pb)

    return cards[:3]
