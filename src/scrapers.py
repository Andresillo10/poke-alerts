"""Conectores de scraping y APIs verificadas para Pokémon GO y Pokémon TCG Pocket."""

from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import HTTP_HEADERS, REQUEST_TIMEOUT_SEC, URLS
from src.models import AlertItem, GameType

LOGGER = logging.getLogger(__name__)

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", flags=re.IGNORECASE)
_FULL_DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})")
_MONTH_DAY_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2})")
_DAY_YEAR_RE = re.compile(r"(\d{1,2})\s+(\d{4})")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass(frozen=True)
class FetchResult:
    """Resultado encapsulado por fuente para aislar fallos individuales."""

    source_name: str
    items: list[AlertItem]
    ok: bool
    error: str | None = None


def create_session() -> requests.Session:
    """Crea una sesión HTTP reutilizable con encabezados estándar y reintentos cortos."""
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    retry = Retry(
        total=2,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_text_safe(session: requests.Session, url: str) -> str:
    """Descarga texto HTTP detectando Cloudflare y corrigiendo codificación UTF-8."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT_SEC)
    if resp.status_code == 403 or "Just a moment..." in resp.text[:400]:
        raise PermissionError(f"Bloqueo HTTP {resp.status_code} (posible Cloudflare) en {url}")
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _fetch_text(session: requests.Session, url: str) -> str:
    """Alias interno compatible hacia fetch_text_safe."""
    return fetch_text_safe(session, url)


def parse_serebii_end_date(duration_text: str) -> date | None:
    """Extrae la fecha de fin de un rango Serebii (incluyendo eventos de 1 día con hora)."""
    clean = _ORDINAL_RE.sub(r"\1", duration_text).replace(",", " ")
    all_full = _FULL_DATE_RE.findall(clean)
    if all_full:
        month_name, day_s, year_s = all_full[-1]
        month = _MONTHS.get(month_name.lower())
        if month:
            return _safe_date(int(year_s), month, int(day_s))

    parts = [part.strip() for part in clean.split("-")]
    if len(parts) >= 2:
        head_match = _MONTH_DAY_RE.search(parts[0])
        tail_day_year = _DAY_YEAR_RE.search(parts[-1])
        if head_match and tail_day_year:
            month = _MONTHS.get(head_match.group(1).lower())
            if month:
                return _safe_date(int(tail_day_year.group(2)), month, int(tail_day_year.group(1)))
    return None


def parse_serebii_start_date(duration_text: str, end_dt: date | None = None) -> date | None:
    """Extrae la fecha de inicio de un rango Serebii."""
    clean = _ORDINAL_RE.sub(r"\1", duration_text).replace(",", " ")
    head_part = clean.split("-")[0].strip()
    full_head = _FULL_DATE_RE.search(head_part)
    if full_head:
        month = _MONTHS.get(full_head.group(1).lower())
        if month:
            return _safe_date(int(full_head.group(3)), month, int(full_head.group(2)))

    head_match = _MONTH_DAY_RE.search(head_part)
    if head_match and end_dt:
        month = _MONTHS.get(head_match.group(1).lower())
        if month:
            year = end_dt.year - 1 if month > end_dt.month else end_dt.year
            return _safe_date(year, month, int(head_match.group(2)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Construye una fecha validando rango calendario sin lanzar excepciones."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_serebii_start_date(duration_text: str, end_dt: date | None = None) -> date | None:
    """Extrae la fecha de inicio de un rango Serebii."""
    clean = _ORDINAL_RE.sub(r"\1", duration_text).replace(",", " ")
    head_part = clean.split("-")[0].strip()
    full_head = _FULL_DATE_RE.search(head_part)
    if full_head:
        month = _MONTHS.get(full_head.group(1).lower())
        if month:
            return _safe_date(int(full_head.group(3)), month, int(full_head.group(2)))

    head_match = _MONTH_DAY_RE.search(head_part)
    if head_match and end_dt:
        month = _MONTHS.get(head_match.group(1).lower())
        if month:
            year = end_dt.year - 1 if month > end_dt.month else end_dt.year
            return _safe_date(year, month, int(head_match.group(2)))
    return None


def _is_iso_expired(iso_str: str | None, now_utc: datetime) -> bool:
    """Determina si una fecha ISO-8601 ya venció frente al instante UTC actual."""
    if not iso_str:
        return False
    try:
        normalized = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < now_utc
    except ValueError:
        return False


def _is_leekduck_expired(card: BeautifulSoup, now_utc: datetime) -> bool:
    """Determina si una tarjeta promo de LeekDuck ya expiró por clase o data-expires."""
    classes = [c.lower() for c in (card.get("class") or [])]
    if "expired" in classes:
        return True

    expiry_el = card.select_one("span.expiry")
    if not expiry_el:
        return False

    raw_exp = (expiry_el.get("data-expires") or "").strip()
    if raw_exp:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw_exp, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt < now_utc
            except ValueError:
                continue

    return "expired" in expiry_el.get_text(" ", strip=True).lower()


def fetch_leekduck_promos(
    session: requests.Session,
    now_utc: datetime | None = None,
) -> FetchResult:
    """Extrae únicamente códigos promocionales vigentes de Pokémon GO en Leek Duck."""
    source = "LeekDuck Promo"
    ref_now = now_utc or datetime.now(timezone.utc)
    try:
        html = _fetch_text(session, URLS["leekduck_promo"])
        return parse_leekduck_promos_html(html, now_utc=ref_now, source=source)
    except Exception as exc:
        LOGGER.warning("[%s] Fallo: %s", source, exc)
        return FetchResult(source, [], ok=False, error=str(exc))


def parse_leekduck_promos_html(
    html: str,
    *,
    now_utc: datetime | None = None,
    source: str = "LeekDuck Promo",
) -> FetchResult:
    """Parsea tarjetas HTML de LeekDuck filtrando códigos expirados."""
    ref_now = now_utc or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "html.parser")
    items: list[AlertItem] = []

    for card in soup.select(".promo-card"):
        if _is_leekduck_expired(card, ref_now):
            continue

        code_el = card.select_one(".code-display p.text")
        title_el = card.select_one("h3.title")
        if not code_el or not title_el:
            continue

        code = code_el.get_text(strip=True)
        title = title_el.get_text(" ", strip=True)
        desc_el = card.select_one(".description p")
        summary = desc_el.get_text(" ", strip=True) if desc_el else title

        rewards_list = [r.get_text(" ", strip=True) for r in card.select(".reward-label")]
        rewards = ", ".join(r for r in rewards_list if r) or None

        expiry_el = card.select_one("span.expiry")
        expires_at = None
        if expiry_el:
            raw_exp_txt = expiry_el.get_text(" ", strip=True)
            expires_at = re.sub(r"^Expires:\s*", "", raw_exp_txt, flags=re.IGNORECASE).strip() or None

        redeem_el = card.select_one("a.link-button[href]")
        redeem_url = (
            redeem_el["href"].strip()
            if redeem_el
            else f"{URLS['pogo_redeem_base']}?passcode={code}"
        )

        items.append(
            AlertItem(
                uid=f"leekduck-promo:{code}",
                game="pogo",
                kind="code",
                title=title,
                summary=summary,
                url=URLS["leekduck_promo"],
                source_name=source,
                promo_code=code,
                redeem_url=redeem_url,
                rewards=rewards,
                expires_at=expires_at,
                language="en",
            )
        )
    return FetchResult(source, items, ok=True)


def fetch_scrapedduck_events(session: requests.Session, now_utc: datetime | None = None) -> FetchResult:
    """Extrae eventos activos y próximos de Pokémon GO desde ScrapedDuck JSON."""
    source = "ScrapedDuck JSON"
    now = now_utc or datetime.now(timezone.utc)
    try:
        resp = session.get(URLS["scrapedduck_events"], timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        events = resp.json()

        items: list[AlertItem] = []
        for ev in events:
            name = (ev.get("name") or "").strip()
            event_id = (ev.get("eventID") or "").strip()
            if not name or not event_id or "[canceled]" in name.lower():
                continue
            if _is_iso_expired(ev.get("end"), now):
                continue

            heading = (ev.get("heading") or "Evento").strip()
            start = (ev.get("start") or "")[:10]
            end = (ev.get("end") or "")[:10]
            window = f"{start} al {end}".strip(" al") if (start or end) else None
            starts_on = start if len(start) == 10 and start[4] == "-" else None

            items.append(
                AlertItem(
                    uid=f"scrapedduck:{event_id}",
                    game="pogo",
                    kind="event",
                    title=name,
                    summary=(
                        f"{heading} in Pokémon GO. Check bonuses, spawns, and tasks before it ends!"
                    ),
                    url=(ev.get("link") or URLS["scrapedduck_events"]).strip(),
                    source_name=source,
                    expires_at=window,
                    language="en",
                    starts_on=starts_on,
                )
            )
        return FetchResult(source, items, ok=True)
    except Exception as exc:
        LOGGER.warning("[%s] Fallo: %s", source, exc)
        return FetchResult(source, [], ok=False, error=str(exc))


def fetch_pogolive_es_news(session: requests.Session) -> FetchResult:
    """Extrae noticias oficiales en español desde pokemongolive.com/es-MX/news."""
    source = "Pokémon GO Live (ES)"
    try:
        html = _fetch_text(session, URLS["pogolive_es_news"])
        soup = BeautifulSoup(html, "html.parser")
        items: list[AlertItem] = []
        seen_hrefs: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if "/news/" not in href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            title_div = anchor.find(class_=re.compile(r"newsCardTitle"))
            title = (
                title_div.get_text(" ", strip=True)
                if title_div
                else anchor.get_text(" ", strip=True)
            )
            if not title:
                continue

            date_el = anchor.find("pg-date-format")
            date_label = None
            if date_el and date_el.get("timestamp"):
                try:
                    ts = int(date_el["timestamp"]) / 1000.0
                    date_label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    date_label = None

            full_url = urljoin("https://pokemongolive.com", href)
            items.append(
                AlertItem(
                    uid=f"pogolive:{href}",
                    game="pogo",
                    kind="news",
                    title=title,
                    summary=f"Anuncio oficial publicado en Pokémon GO Live: {title}",
                    url=full_url,
                    source_name=source,
                    expires_at=date_label,
                    language="es",
                )
            )
            if len(items) >= 10:
                break

        return FetchResult(source, items, ok=True)
    except Exception as exc:
        LOGGER.warning("[%s] Fallo: %s", source, exc)
        return FetchResult(source, [], ok=False, error=str(exc))


def fetch_serebii_events(
    session: requests.Session,
    *,
    url: str,
    game: GameType,
    source_name: str,
    today: date | None = None,
) -> FetchResult:
    """Conector compartido para tablas de eventos de Serebii (Pokémon GO y TCG Pocket)."""
    ref_today = today or datetime.now(timezone.utc).date()
    try:
        html = _fetch_text(session, url)
        soup = BeautifulSoup(html, "html.parser")
        items: list[AlertItem] = []

        for tr in soup.select("table.dextab tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) != 3 or "fooinfo" not in (tds[1].get("class") or []):
                continue

            link_el = tds[1].find("a", href=True)
            title = tds[1].get_text(" ", strip=True)
            duration = tds[2].get_text(" ", strip=True)
            if not title:
                continue

            end_dt = parse_serebii_end_date(duration)
            if end_dt and end_dt < ref_today:
                continue
            start_dt = parse_serebii_start_date(duration, end_dt=end_dt)

            href = link_el["href"].strip() if link_el else url
            full_url = urljoin(url, href)
            summary_en = (
                "Active event in Pokémon TCG Pocket! Open the game to check missions, promo cards, and rewards."
                if game == "tcgp"
                else "Featured event in Pokémon GO! Check bonuses, spawns, and rewards in the link."
            )
            items.append(
                AlertItem(
                    uid=f"{source_name.lower().replace(' ', '-')}:{full_url}",
                    game=game,
                    kind="event",
                    title=title,
                    summary=summary_en,
                    url=full_url,
                    source_name=source_name,
                    expires_at=duration or None,
                    language="en",
                    starts_on=start_dt.isoformat() if start_dt else None,
                )
            )

        return FetchResult(source_name, items, ok=True)
    except Exception as exc:
        LOGGER.warning("[%s] Fallo: %s", source_name, exc)
        return FetchResult(source_name, [], ok=False, error=str(exc))


_WP_TAG_RE = re.compile(r"<[^>]+>")


def parse_ptcgpocket_wp_posts(posts: list[dict], source: str = "PTCGPocket.gg") -> list[AlertItem]:
    """Convierte posts WP JSON (eventos, noticias, expansiones, pase) a AlertItem."""
    items: list[AlertItem] = []
    for post in posts:
        href = (post.get("link") or "").strip()
        raw_title = (post.get("title") or {}).get("rendered") or ""
        title = html_lib.unescape(_WP_TAG_RE.sub("", raw_title)).strip()
        if not href or not title:
            continue

        cats = set(post.get("categories") or [])
        kind = "event" if 7 in cats else "news"
        date_text = (post.get("date") or "")[:10] or None
        items.append(
            AlertItem(
                uid=f"ptcgpocket-gg:{href}",
                game="tcgp",
                kind=kind,
                title=title,
                summary=f"Official update in Pokémon TCG Pocket: {title}",
                url=href,
                source_name=source,
                expires_at=date_text,
                language="en",
            )
        )
    return items


def fetch_ptcgpocket_gg_events(session: requests.Session) -> FetchResult:
    """Extrae eventos y noticias recientes de TCG Pocket vía WP API con fallback HTML."""
    source = "PTCGPocket.gg"
    try:
        wp_resp = session.get(
            URLS["ptcgpocket_gg_wp_search"],
            params={
                "per_page": 10,
                "categories": URLS["ptcgpocket_gg_wp_categories"],
                "_fields": "date,link,title,categories",
            },
            timeout=REQUEST_TIMEOUT_SEC,
        )
        if wp_resp.ok:
            wp_items = parse_ptcgpocket_wp_posts(wp_resp.json(), source=source)
            if wp_items:
                return FetchResult(source, wp_items, ok=True)
    except Exception as exc:
        LOGGER.warning("[%s WP API] Probando fallback HTML (%s).", source, exc)

    try:
        html = _fetch_text(session, URLS["ptcgpocket_gg_events"])
        soup = BeautifulSoup(html, "html.parser")
        items: list[AlertItem] = []

        for article in soup.select("article.entry-card")[:10]:
            link_el = article.select_one("h2.entry-title a[href]")
            if not link_el:
                continue

            title = link_el.get_text(" ", strip=True)
            href = link_el["href"].strip()
            date_el = article.select_one("li.meta-date time, li.meta-date")
            date_text = date_el.get_text(" ", strip=True) if date_el else None

            items.append(
                AlertItem(
                    uid=f"ptcgpocket-gg:{href}",
                    game="tcgp",
                    kind="event",
                    title=title,
                    summary=f"Evento en Pokémon TCG Pocket: {title}",
                    url=href,
                    source_name=source,
                    expires_at=date_text,
                    language="en",
                )
            )

        return FetchResult(source, items, ok=True)
    except Exception as exc:
        LOGGER.warning("[%s] Fallo: %s", source, exc)
        return FetchResult(source, [], ok=False, error=str(exc))


def _parse_short_english_date(raw: str) -> date | None:
    """Parsea fechas cortas estilo 'Jul 5, 2025' o 'December 31, 2027'."""
    clean = _ORDINAL_RE.sub(r"\1", (raw or "").replace(",", " "))
    m = _FULL_DATE_RE.search(clean)
    if not m:
        return None
    month_txt, day_s, year_s = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    short_map = {k[:3]: v for k, v in _MONTHS.items()}
    month = _MONTHS.get(month_txt) or short_map.get(month_txt[:3])
    return _safe_date(year_s, month, day_s) if month else None


def parse_ptcgpocket_codes_html(
    html: str,
    *,
    now_utc: datetime | None = None,
    source: str = "PTCGPocket Codes",
) -> FetchResult:
    """Extrae códigos activos de Pokémon TCG Pocket descartando expirados."""
    ref_today = (now_utc or datetime.now(timezone.utc)).date()
    soup = BeautifulSoup(html, "html.parser")
    items: list[AlertItem] = []

    for tbl in soup.select("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in tbl.select("tr th")]
        if not headers or "code" not in headers[0]:
            continue
        for tr in tbl.select("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.select("td")]
            if len(tds) < 4:
                continue
            code, rel_txt, reward_txt, exp_txt = tds[0], tds[1], tds[2], tds[3]
            if not code or " " in code or len(code) < 5:
                continue
            exp_dt = _parse_short_english_date(exp_txt)
            if exp_dt and exp_dt < ref_today:
                continue
            start_dt = _parse_short_english_date(rel_txt)
            items.append(
                AlertItem(
                    uid=f"ptcgpocket-code:{code}",
                    game="tcgp",
                    kind="code",
                    title=f"Pokémon TCG Pocket Gift Code: {code}",
                    summary=f"Canjea este código en el portal oficial de TCG Pocket: {reward_txt}.",
                    url=URLS["ptcgpocket_gg_codes"],
                    source_name=source,
                    promo_code=code,
                    redeem_url=URLS["tcgp_redeem_url"],
                    rewards=reward_txt or None,
                    expires_at=exp_dt.isoformat() if exp_dt else (exp_txt or None),
                    starts_on=start_dt.isoformat() if start_dt else None,
                    language="en",
                )
            )
    return FetchResult(source, items, ok=True)


def fetch_ptcgpocket_codes(
    session: requests.Session,
    now_utc: datetime | None = None,
) -> FetchResult:
    """Descarga códigos promocionales activos de Pokémon TCG Pocket desde PTCGPocket.gg."""
    source = "PTCGPocket Codes"
    try:
        html = _fetch_text(session, URLS["ptcgpocket_gg_codes"])
        return parse_ptcgpocket_codes_html(html, now_utc=now_utc, source=source)
    except Exception as exc:
        LOGGER.warning("[%s] Fallo: %s", source, exc)
        return FetchResult(source, [], ok=False, error=str(exc))


def fetch_all_sources(session: requests.Session) -> list[FetchResult]:
    """Ejecuta todos los conectores verificados aislando fallos por fuente."""
    return [
        fetch_leekduck_promos(session),
        fetch_scrapedduck_events(session),
        fetch_pogolive_es_news(session),
        fetch_serebii_events(
            session,
            url=URLS["serebii_pogo_events"],
            game="pogo",
            source_name="Serebii GO",
        ),
        fetch_serebii_events(
            session,
            url=URLS["serebii_tcgp_events"],
            game="tcgp",
            source_name="Serebii TCGP",
        ),
        fetch_ptcgpocket_gg_events(session),
        fetch_ptcgpocket_codes(session),
    ]
