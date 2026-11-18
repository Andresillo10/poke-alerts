"""Agregador semanal ($0) de torneos Estándar para Top 3 mazos Ranked en TCG Pocket."""

from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

from src.config import REQUEST_TIMEOUT_SEC
from src.deck_qr import (
    BuiltDeck,
    CardCatalog,
    CardRecord,
    EventDeckConstraints,
    encode_deck_payload,
    infer_energy_types,
    validate_deck,
)

LOGGER = logging.getLogger(__name__)

LIMITLESS_BASE_URL = "https://play.limitlesstcg.com"
LIMITLESS_TOURNAMENTS_URL = f"{LIMITLESS_BASE_URL}/tournaments/completed"
_SCORE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)\s*-\s*(\d+)$")
_CARD_LINE_RE = re.compile(r"^(\d+)\s+(.+?)(?:\s+\([A-Z0-9-]+\))?$")


@dataclass(frozen=True)
class TournamentSummary:
    """Metadatos de un torneo Estándar completado en Limitless TCG Pocket."""

    tournament_id: str
    name: str
    played_at: datetime
    players: int
    standings_url: str


@dataclass(frozen=True)
class WeeklyArchetypeSummary:
    """Resumen estadístico semanal de un arquetipo en torneos competitivos."""

    slug: str
    name: str
    entries: int
    wins: int
    losses: int
    win_rate: float
    best_note: str
    deck: BuiltDeck


@dataclass(frozen=True)
class WeeklyMetaReport:
    """Reporte semanal completo listo para renderizar y publicar los lunes."""

    window_label: str
    analyzed_tournaments: int
    analyzed_entries: int
    archetypes: tuple[WeeklyArchetypeSummary, ...]

    @property
    def decks(self) -> list[BuiltDeck]:
        """Devuelve la lista ordenada de barajas del Top 3."""
        return [item.deck for item in self.archetypes]


def parse_completed_tournaments_html(
    html: str,
    *,
    cutoff_utc: datetime,
    min_players: int = 24,
) -> tuple[list[TournamentSummary], bool]:
    """Extrae torneos Estándar recientes de la tabla de torneos completados."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[TournamentSummary] = []
    reached_cutoff = False

    for tr in soup.select("table tr[data-date]"):
        raw_date = str(tr.get("data-date") or "").strip()
        if not raw_date:
            continue
        try:
            played_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            continue
        if played_at < cutoff_utc:
            reached_cutoff = True
            break

        # Solo formato Estándar ("1") para reflejar el metajuego Ranked real
        if str(tr.get("data-format") or "") != "1":
            continue
        try:
            players = int(tr.get("data-players") or 0)
        except ValueError:
            players = 0
        if players < min_players:
            continue

        stand_a = next(
            (a for a in tr.select("a") if "/standings" in (a.get("href") or "")),
            None,
        )
        if not stand_a:
            continue
        href = str(stand_a.get("href") or "")
        parts = [p for p in href.split("/") if p]
        t_id = parts[1] if len(parts) >= 2 else href
        found.append(
            TournamentSummary(
                tournament_id=t_id,
                name=" ".join(str(tr.get("data-name") or "").split()),
                played_at=played_at,
                players=players,
                standings_url=href if href.startswith("http") else f"{LIMITLESS_BASE_URL}{href}",
            )
        )
    return found, reached_cutoff


def parse_tournament_standings_rows(html: str) -> list[dict[str, Any]]:
    """Extrae posiciones, récords y arquetipos de la tabla de resultados."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for tr in soup.select("table tr[data-placing]"):
        try:
            placing = int(tr.get("data-placing") or 999)
        except ValueError:
            placing = 999

        wins, losses = 0, 0
        for td in tr.select("td"):
            m_score = _SCORE_RE.match(td.get_text(" ", strip=True))
            if m_score:
                wins, losses = int(m_score.group(1)), int(m_score.group(2))
                break

        dl_a = next((a for a in tr.select("a") if "/decklist" in (a.get("href") or "")), None)
        meta_links = [a for a in tr.select("a") if "/metagame/" in (a.get("href") or "")]
        if not dl_a or not meta_links:
            continue

        dl_href = str(dl_a.get("href") or "")
        decklist_url = dl_href if dl_href.startswith("http") else f"{LIMITLESS_BASE_URL}{dl_href}"
        for meta_a in meta_links:
            slug = str(meta_a.get("href") or "").split("/metagame/")[-1]
            tip = meta_a.select_one("[data-tooltip]")
            archetype_name = str(tip.get("data-tooltip") or slug) if tip else slug
            rows.append(
                {
                    "placing": placing,
                    "wins": wins,
                    "losses": losses,
                    "slug": slug,
                    "name": archetype_name.strip(),
                    "decklist_url": decklist_url,
                }
            )
    return rows


def _resolve_card_from_link(href: str, name: str, catalog: CardCatalog) -> CardRecord | None:
    """Resuelve un enlace `/cards/<SET>/<NUM>` de Limitless contra PocketDecks v5."""
    clean_name = name.replace("\u2019", "'").replace("\u2018", "'").strip()
    parts = [p for p in href.rstrip("/").split("/") if p]
    if len(parts) >= 2:
        set_clean = parts[-2].replace("-", "").lower()
        num_raw = parts[-1]
        cid = f"{set_clean}-{int(num_raw):03d}" if num_raw.isdigit() else f"{set_clean}-{num_raw.lower()}"
        rec = catalog.by_card_id.get(cid) or catalog.by_set_name.get((set_clean, clean_name.lower()))
        if rec:
            return rec
    candidates = catalog.by_name.get(clean_name.lower())
    return candidates[0] if candidates else None


def parse_limitless_decklist_cards(
    html: str,
    *,
    target_archetype_name: str,
    catalog: CardCatalog,
) -> list[CardRecord]:
    """Extrae exactamente 20 cartas resolviendo torneos normales y Dual-Deck."""
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select(".decklist")
    if not blocks:
        blocks = [soup]

    target_tokens = {tok for tok in re.findall(r"[a-z0-9]+", target_archetype_name.lower()) if len(tok) > 2}
    best_cards: list[CardRecord] = []
    best_overlap = -1

    for blk in blocks:
        resolved: list[CardRecord] = []
        valid = True
        for a in blk.select("a[href*='/cards/']"):
            text = " ".join(a.get_text(" ", strip=True).split())
            m_line = _CARD_LINE_RE.match(text)
            qty = int(m_line.group(1)) if m_line else 1
            cname = m_line.group(2) if m_line else text
            rec = _resolve_card_from_link(str(a.get("href") or ""), cname, catalog)
            if not rec:
                valid = False
                break
            resolved.extend([rec] * qty)
        if not valid or len(resolved) != 20:
            continue

        pk_names = " ".join(c.name.lower() for c in resolved if c.is_pokemon)
        overlap = sum(1 for tok in target_tokens if tok in pk_names)
        if overlap > best_overlap:
            best_overlap = overlap
            best_cards = resolved

    return best_cards


def fetch_weekly_standard_tournaments(
    session: requests.Session,
    *,
    now_utc: datetime,
    days: int = 7,
    min_players: int = 24,
    max_pages: int = 4,
) -> list[TournamentSummary]:
    """Recorre las páginas recientes de Limitless dentro de la ventana semanal."""
    cutoff_utc = now_utc - timedelta(days=days)
    tournaments: list[TournamentSummary] = []

    for page in range(1, max_pages + 1):
        resp = session.get(
            LIMITLESS_TOURNAMENTS_URL,
            params={"game": "POCKET", "page": page},
            timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        batch, reached_cutoff = parse_completed_tournaments_html(
            resp.text,
            cutoff_utc=cutoff_utc,
            min_players=min_players,
        )
        tournaments.extend(batch)
        if reached_cutoff:
            break
        time.sleep(0.2)

    tournaments.sort(key=lambda t: (t.players, t.played_at), reverse=True)
    return tournaments


def build_weekly_ranked_report(
    session: requests.Session,
    catalog: CardCatalog,
    *,
    now_utc: datetime | None = None,
    days: int = 7,
    max_tournaments: int = 12,
) -> WeeklyMetaReport | None:
    """Agrega los torneos Estándar semanales y genera el Top 3 con códigos QR."""
    ref_utc = now_utc or datetime.now(timezone.utc)
    tournaments = fetch_weekly_standard_tournaments(session, now_utc=ref_utc, days=days, min_players=24)
    if len(tournaments) < 3:
        tournaments = fetch_weekly_standard_tournaments(session, now_utc=ref_utc, days=days, min_players=12)
    selected = tournaments[:max_tournaments]
    if not selected:
        return None

    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "entries": 0, "wins": 0, "losses": 0, "score": 0.0, "candidates": []}
    )
    total_entries = 0

    for tourn in selected:
        try:
            time.sleep(0.2)
            resp = session.get(tourn.standings_url, timeout=REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            rows = parse_tournament_standings_rows(resp.text)
        except Exception as exc:
            LOGGER.warning("[WeeklyMeta] Error leyendo %s: %s", tourn.standings_url, exc)
            continue

        total_entries += len(rows)
        for row in rows:
            placing = row["placing"]
            wins, losses = row["wins"], row["losses"]
            st = agg[row["slug"]]
            st["name"] = row["name"]
            st["entries"] += 1
            st["wins"] += wins
            st["losses"] += losses
            place_bonus = max(0.5, 4.0 - math.log2(max(placing, 1)))
            st["score"] += math.sqrt(tourn.players) * (place_bonus + 0.15 * wins)
            rank_key = (placing, -tourn.players, -(wins - losses))
            note = f"#{placing}/{tourn.players}p ({wins}-{losses}) en {tourn.name[:32]}"
            st["candidates"].append((rank_key, row["decklist_url"], note))

    ranked_archs = sorted(agg.items(), key=lambda kv: kv[1]["score"], reverse=True)
    # Filtra outliers con 1 sola aparición salvo que no haya suficientes arquetipos
    primary_archs = [kv for kv in ranked_archs if kv[1]["entries"] >= 3]
    ordered_pool = primary_archs if len(primary_archs) >= 3 else ranked_archs

    top_items: list[WeeklyArchetypeSummary] = []
    unrestricted = EventDeckConstraints(is_combat_event=True)

    for rank_idx, (slug, st) in enumerate(ordered_pool):
        if len(top_items) == 3:
            break
        candidates = sorted(st["candidates"], key=lambda x: x[0])[:4]
        chosen_deck: BuiltDeck | None = None
        chosen_note = ""
        wr = st["wins"] / max(st["wins"] + st["losses"], 1) * 100.0

        for _, dl_url, note in candidates:
            try:
                time.sleep(0.15)
                dl_resp = session.get(dl_url, timeout=REQUEST_TIMEOUT_SEC)
                dl_resp.raise_for_status()
                cards = parse_limitless_decklist_cards(
                    dl_resp.text,
                    target_archetype_name=st["name"],
                    catalog=catalog,
                )
                if len(cards) != 20:
                    continue
                pks = [c for c in cards if c.is_pokemon]
                trs = [c for c in cards if not c.is_pokemon]
                energies = infer_energy_types(pks)
                ok, reason = validate_deck(cards, energies, unrestricted)
                if not ok:
                    LOGGER.debug("[WeeklyMeta] Lista rechazada (%s): %s", st["name"], reason)
                    continue
                chosen_deck = BuiltDeck(
                    title=f"#{len(top_items) + 1} {st['name']}",
                    mission_tag=f"{wr:.1f}% WR • {st['entries']} apariciones • {note.split(' en ')[0]}",
                    cards=tuple(cards),
                    energy_types=tuple(energies),
                    qr_payload=encode_deck_payload(
                        [c.qr_id for c in trs],
                        [c.qr_id for c in pks],
                        energies,
                    ),
                )
                chosen_note = note
                break
            except Exception as exc:
                LOGGER.warning("[WeeklyMeta] Error descargando decklist %s: %s", dl_url, exc)

        if chosen_deck:
            top_items.append(
                WeeklyArchetypeSummary(
                    slug=slug,
                    name=st["name"],
                    entries=st["entries"],
                    wins=st["wins"],
                    losses=st["losses"],
                    win_rate=wr,
                    best_note=chosen_note,
                    deck=chosen_deck,
                )
            )

    if not top_items:
        return None

    start_lbl = (ref_utc - timedelta(days=days)).strftime("%d/%m")
    end_lbl = ref_utc.strftime("%d/%m/%Y")
    return WeeklyMetaReport(
        window_label=f"{start_lbl} – {end_lbl} ({len(selected)} torneos Estándar)",
        analyzed_tournaments=len(selected),
        analyzed_entries=total_entries,
        archetypes=tuple(top_items),
    )
