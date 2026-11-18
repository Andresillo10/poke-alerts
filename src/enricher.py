"""Enriquecedor v2.0 de detalles reales (misiones, spawns, bonus y guías de mazos)."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import replace
from typing import Any

import requests
from bs4 import BeautifulSoup

from src.config import REQUEST_TIMEOUT_SEC, URLS
from src.models import AlertItem
from src.scrapers import fetch_text_safe

LOGGER = logging.getLogger(__name__)
_TAG_RE = re.compile(r"<[^>]+>")
_STOPWORDS = {"the", "and", "with", "for", "event", "events", "pokemon", "tcg", "pocket", "2025", "2026", "2027"}


def _clean_text(text: str, limit: int = 210) -> str:
    """Normaliza espacios y acorta texto de detalle manteniendo frases enteras cuando cabe."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def _dedupe_keep_order(values: list[str], limit: int) -> list[str]:
    """Elimina duplicados preservando el orden original."""
    seen: set[str] = set()
    out: list[str] = []
    for val in values:
        key = val.lower()
        if val and key not in seen:
            seen.add(key)
            out.append(val)
            if len(out) >= limit:
                break
    return out


def parse_serebii_detail_html(detail_html: str) -> tuple[str | None, list[str]]:
    """Extrae resumen, bonus, Pokémon salvajes, misiones y cartas/sobres de Serebii."""
    soup = BeautifulSoup(detail_html, "html.parser")
    root = soup.select_one("main") or soup

    intro: str | None = None
    for p in root.find_all("p"):
        txt = " ".join(p.get_text(" ", strip=True).split())
        if len(txt) >= 35 and "serebii.net" not in txt.lower():
            intro = _clean_text(txt, limit=230)
            break

    bonuses: list[str] = []
    spawns: list[str] = []
    tasks: list[str] = []
    cards_or_packs: list[str] = []

    for tbl in root.select("table.dextab, table.dextable"):
        trs = tbl.find_all("tr", recursive=False)
        if not trs:
            tbody = tbl.find("tbody", recursive=False)
            trs = tbody.find_all("tr", recursive=False) if tbody else []
        if len(trs) < 2:
            continue

        header = [c.get_text(" ", strip=True).lower() for c in trs[0].find_all(["th", "td"], recursive=False)]
        if header == ["effect", "type"]:
            for tr in trs[1:]:
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
                if len(cols) >= 2 and cols[0]:
                    bonuses.append(f"{cols[0]} ({cols[1]})" if cols[1] else cols[0])

        elif header[:4] == ["no.", "pic", "name", "type"] and len(header) <= 5:
            for tr in trs[1:]:
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
                if len(cols) >= 3 and cols[2]:
                    spawns.append(cols[2])

        elif header and header[0] == "task":
            for tr in trs[1:]:
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
                if not cols or not cols[0]:
                    continue
                reward = next((c for c in cols[1:] if c and not c.isdigit()), "")
                tasks.append(f"{cols[0]} → {reward}" if reward else cols[0])

        elif "card name" in header or "booster packs" in header:
            idx = header.index("card name") if "card name" in header else header.index("booster packs")
            for tr in trs[1:]:
                cols = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
                if len(cols) > idx and cols[idx]:
                    cards_or_packs.append(cols[idx])

    bullets: list[str] = []
    if bonuses:
        bullets.append("Bonuses: " + "; ".join(_dedupe_keep_order(bonuses, 3)))
    if spawns:
        bullets.append("Wild spawns: " + ", ".join(_dedupe_keep_order(spawns, 6)))
    if tasks:
        bullets.append("Missions: " + " | ".join(_dedupe_keep_order(tasks, 3)))
    if cards_or_packs:
        bullets.append("Featured cards/packs: " + ", ".join(_dedupe_keep_order(cards_or_packs, 4)))

    return intro, bullets


def _infer_reward_from_cell(cell) -> str:
    """Extrae recompensa textual o desde nombre de icono en tablas de PTCGPocket.gg."""
    txt = cell.get_text(" ", strip=True)
    if txt:
        return txt
    img = cell.find("img")
    if not img:
        return ""
    hint = f"{img.get('alt', '')} {img.get('src', '')}".lower()
    if "shopticket" in hint:
        return "Cupón de evento"
    if "hourglass" in hint:
        return "Relojes de Arena"
    if "shinedust" in hint or "stardust" in hint:
        return "Polvo Iris"
    if "promo" in hint or "pack" in hint:
        return "Sobre Promo"
    if "wonder" in hint:
        return "Reloj Mágico"
    return ""


def parse_ptcgpocket_detail_html(detail_html: str) -> tuple[str | None, list[str]]:
    """Extrae descripción y misiones clave reales de un artículo de PTCGPocket.gg."""
    soup = BeautifulSoup(detail_html, "html.parser")
    content = soup.select_one(".entry-content") or soup

    intro: str | None = None
    for p in content.find_all("p"):
        txt = " ".join(p.get_text(" ", strip=True).split())
        if len(txt) >= 40 and "everything you need to know" not in txt.lower():
            intro = _clean_text(txt, limit=230)
            break

    skip_heads = {"mission", "missions", "mission type", "wins", "task", "day", "rank", "type", "tier", "card", "event"}
    skip_tails = {"reward", "rewards", "mission type", "win points", "quantity", "amount", "progress"}
    missions: list[str] = []
    for tbl in content.select("table"):
        trs = tbl.find_all("tr")
        if not trs:
            continue
        hdr_cells = [c.get_text(" ", strip=True).lower() for c in trs[0].find_all(["th", "td"], recursive=False)]
        prog_idx = hdr_cells.index("progress") if "progress" in hdr_cells else -1
        amt_idx = hdr_cells.index("amount") if "amount" in hdr_cells else -1
        for tr in trs:
            raw_cells = tr.find_all(["th", "td"], recursive=False)
            if len(raw_cells) < 2:
                continue
            texts = [c.get_text(" ", strip=True) for c in raw_cells]
            if not texts[0] or texts[0].lower() in skip_heads or texts[-1].lower() in skip_tails:
                continue
            reward_parts: list[str] = []
            task_txt = texts[0]
            # Tablas estilo Day | Mission Type | Reward (donde la última columna no es Progress)
            if len(raw_cells) == 3 and prog_idx == -1 and texts[1] and texts[2] and not texts[2].isdigit():
                task_txt = f"{texts[0]} ({texts[1]})"
                reward_txt = _infer_reward_from_cell(raw_cells[2])
                if reward_txt:
                    reward_parts.append(reward_txt)
            else:
                for idx_c, cell in enumerate(raw_cells[1:], 1):
                    if idx_c == prog_idx or re.fullmatch(r"\d+/\d+", cell.get_text(strip=True)):
                        continue
                    val = _infer_reward_from_cell(cell)
                    if val:
                        if idx_c == amt_idx and val.isdigit() and reward_parts:
                            reward_parts[-1] = f"{val}× {reward_parts[-1]}"
                        else:
                            reward_parts.append(val)
            reward_str = " ".join(reward_parts).strip()
            missions.append(f"{task_txt} → {reward_str}" if reward_str else task_txt)

    bullets: list[str] = []
    if missions:
        bullets.append("Missions: " + " | ".join(_dedupe_keep_order(missions, 4)))
    return intro, bullets


def _extract_search_keywords(title: str) -> str:
    """Extrae 2 palabras clave del título para consultar guías de mazos."""
    tokens = re.findall(r"[A-Za-z0-9]+", title)
    kept = [t for t in tokens if t.lower() not in _STOPWORDS and len(t) > 2]
    return " ".join(kept[:2]) or title[:25]


def select_best_deck_post(
    posts: list[dict[str, Any]],
    *,
    exclude_url: str,
) -> tuple[str, str] | None:
    """Selecciona únicamente guías reales de mazos o Tier List desde WP JSON."""
    best: tuple[int, str, str] | None = None
    for post in posts:
        link = (post.get("link") or "").strip()
        raw_title = (post.get("title") or {}).get("rendered") or ""
        title = html.unescape(_TAG_RE.sub("", raw_title)).strip()
        if not link or not title or link.rstrip("/") == exclude_url.rstrip("/"):
            continue

        lower = title.lower()
        if "deck" not in lower and "tier list" not in lower:
            continue

        score = 5 if "deck" in lower else 0
        if "tier list" in lower:
            score += 4
        if "best" in lower or "meta" in lower:
            score += 2
        if best is None or score > best[0]:
            best = (score, link, title)

    return (best[1], best[2]) if best else None


def find_tcgp_deck_guide(session: requests.Session, item: AlertItem) -> tuple[str, str]:
    """Busca guía de mazos en PTCGPocket.gg o devuelve enlace al Tier List oficial."""
    query = _extract_search_keywords(item.title)
    try:
        resp = session.get(
            URLS["ptcgpocket_gg_wp_search"],
            params={"per_page": 5, "search": query, "_fields": "link,title"},
            timeout=REQUEST_TIMEOUT_SEC,
        )
        if resp.ok:
            picked = select_best_deck_post(resp.json(), exclude_url=item.url)
            if picked:
                return picked
    except Exception as exc:
        LOGGER.warning("[Enricher Deck] No se pudo consultar guías para '%s': %s", item.title, exc)

    return URLS["ptcgpocket_gg_tier_list"], "Mejores Mazos y Tier List (PTCGPocket.gg)"


def parse_scrapedduck_extra(ev_data: dict[str, Any]) -> list[str]:
    """Extrae bonificaciones, Pokémon, incursiones y variocolor desde events.json."""
    extra = ev_data.get("extraData") or {}
    bullets: list[str] = []
    bonuses: list[str] = []
    featured: list[str] = []
    shinies: list[str] = []

    spotlight = extra.get("spotlight") or {}
    if spotlight:
        if spotlight.get("bonus"):
            bonuses.append(str(spotlight["bonus"]))
        names = [p["name"] for p in (spotlight.get("list") or []) if p.get("name")]
        featured.extend(names)
        if spotlight.get("canBeShiny") and spotlight.get("name"):
            shinies.append(str(spotlight["name"]))

    cday = extra.get("communityday") or {}
    if cday:
        featured.extend(p["name"] for p in (cday.get("spawns") or []) if p.get("name"))
        bonuses.extend(b["text"] for b in (cday.get("bonuses") or []) if b.get("text"))
        shinies.extend(s["name"] for s in (cday.get("shinies") or []) if s.get("name"))

    raids = extra.get("raidbattles") or {}
    if raids:
        featured.extend(b["name"] for b in (raids.get("bosses") or []) if b.get("name"))
        shinies.extend(s["name"] for s in (raids.get("shinies") or []) if s.get("name"))

    if bonuses:
        bullets.append("Bonuses: " + "; ".join(_dedupe_keep_order(bonuses, 3)))
    if featured:
        bullets.append("Featured Pokémon: " + ", ".join(_dedupe_keep_order(featured, 6)))
    if shinies:
        bullets.append("Shinies available: " + ", ".join(_dedupe_keep_order(shinies, 4)))
    return bullets


def _enrich_scrapedduck(
    session: requests.Session,
    item: AlertItem,
    events_cache: dict[str, dict[str, Any]],
) -> tuple[str | None, list[str]]:
    """Enriquece eventos de ScrapedDuck usando events.json y descripción LeekDuck."""
    intro: str | None = None

    if not events_cache:
        try:
            resp = session.get(URLS["scrapedduck_events_full"], timeout=REQUEST_TIMEOUT_SEC)
            if resp.ok:
                for ev in resp.json():
                    if ev.get("eventID"):
                        events_cache[ev["eventID"]] = ev
        except Exception as exc:
            LOGGER.warning("[Enricher ScrapedDuck] Error cargando events.json: %s", exc)

    ev_id = item.uid.removeprefix("scrapedduck:")
    ev_data = events_cache.get(ev_id, {})
    bullets = parse_scrapedduck_extra(ev_data)

    if "leekduck.com" in item.url:
        try:
            raw_html = fetch_text_safe(session, item.url)
            soup = BeautifulSoup(raw_html, "html.parser")
            desc = soup.select_one(".event-description")
            if desc:
                intro = _clean_text(desc.get_text(" ", strip=True), limit=230)
        except Exception as exc:
            LOGGER.warning("[Enricher LeekDuck] Sin detalle extra (%s).", exc)

    return intro, bullets


def _enrich_pogolive_es(session: requests.Session, item: AlertItem) -> str | None:
    """Extrae el primer párrafo útil de una noticia oficial en español de Pokémon GO."""
    try:
        raw_html = fetch_text_safe(session, item.url)
        soup = BeautifulSoup(raw_html, "html.parser")
        for p in soup.select("main p, article p"):
            txt = " ".join(p.get_text(" ", strip=True).split())
            lower = txt.lower()
            if any(bad in lower for bad in ("cookie", "privacidad", "niantic", "©")):
                continue
            if (len(txt) >= 45 and "entrenadores" in lower) or len(txt) >= 65:
                return _clean_text(txt, limit=230)
    except Exception as exc:
        LOGGER.warning("[Enricher POGO Live] Sin detalle extra (%s).", exc)
    return None


def enrich_alert_item(
    session: requests.Session,
    item: AlertItem,
    events_cache: dict[str, dict[str, Any]] | None = None,
) -> AlertItem:
    """Enriquece únicamente alertas de eventos/noticias con misiones, spawns, bonus y mazos."""
    if item.kind == "code":
        return item

    cache = events_cache if events_cache is not None else {}
    intro: str | None = None
    bullets: list[str] = []

    try:
        if "serebii.net" in item.url:
            intro, bullets = parse_serebii_detail_html(fetch_text_safe(session, item.url))
        elif item.uid.startswith("scrapedduck:"):
            intro, bullets = _enrich_scrapedduck(session, item, cache)
        elif "ptcgpocket.gg" in item.url:
            intro, bullets = parse_ptcgpocket_detail_html(fetch_text_safe(session, item.url))
        elif "pokemongolive.com" in item.url:
            intro = _enrich_pogolive_es(session, item)
    except Exception as exc:
        LOGGER.warning("[Enricher] Sin detalle extra para %s: %s", item.uid, exc)

    deck_url = item.deck_guide_url
    deck_title = item.deck_guide_title
    if item.game == "tcgp":
        deck_url, deck_title = find_tcgp_deck_guide(session, item)

    return replace(
        item,
        summary=intro or item.summary,
        guide_bullets=tuple(bullets[:4]),
        deck_guide_url=deck_url,
        deck_guide_title=deck_title,
    )
