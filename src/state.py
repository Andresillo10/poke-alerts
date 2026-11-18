"""Persistencia de estado, deduplicación por tema, recordatorio de inicio y bootstrap."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import (
    HEARTBEAT_INTERVAL_DAYS,
    MAX_ALERTS_PER_RUN,
    MAX_BOOTSTRAP_EVENTS_PER_GAME,
    MAX_SEEN_ITEMS,
    STATE_FILE,
    TOPIC_DEDUP_TTL_DAYS,
)
from src.models import AlertItem, build_topic_key


def default_state() -> dict[str, Any]:
    """Devuelve la estructura base vacía de state.json."""
    return {
        "initialized": False,
        "last_run_utc": None,
        "last_commit_heartbeat_utc": None,
        "seen_uids": {},
        "seen_topics": {},
        "started_notified": {},
        "source_failures": {},
    }


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    """Carga state.json desde disco o inicializa uno limpio si no existe."""
    if not path.exists():
        return default_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    base = default_state()
    base.update(data)
    return base


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    """Guarda state.json podando diccionarios para mantener el archivo liviano."""
    state["seen_uids"] = _prune_dict(state.get("seen_uids", {}), MAX_SEEN_ITEMS)
    state["seen_topics"] = _prune_dict(state.get("seen_topics", {}), MAX_SEEN_ITEMS)
    state["started_notified"] = _prune_dict(state.get("started_notified", {}), MAX_SEEN_ITEMS)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _prune_dict(mapping: dict[str, str], limit: int) -> dict[str, str]:
    """Mantiene únicamente las últimas `limit` entradas de un mapeo cronológico."""
    if len(mapping) <= limit:
        return mapping
    items = list(mapping.items())[-limit:]
    return dict(items)


def _parse_iso_dt(raw_ts: str | None) -> datetime | None:
    """Parsea timestamp ISO-8601 a UTC o devuelve None si es inválido."""
    if not raw_ts:
        return None
    try:
        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso_date(raw_date: str | None) -> date | None:
    """Parsea fecha YYYY-MM-DD o devuelve None."""
    if not raw_date:
        return None
    try:
        return date.fromisoformat(raw_date[:10])
    except ValueError:
        return None


def _is_seen(
    state: dict[str, Any],
    item: AlertItem,
    topic_key: str,
    now_utc: datetime,
) -> bool:
    """Verifica si el UID ya se envió o si el mismo tema salió dentro de la ventana TTL."""
    if item.uid in state["seen_uids"]:
        return True

    topic_ts = _parse_iso_dt(state["seen_topics"].get(topic_key))
    if not topic_ts:
        return False
    return (now_utc - topic_ts) < timedelta(days=TOPIC_DEDUP_TTL_DAYS)


def _should_send_launch_reminder(
    state: dict[str, Any],
    item: AlertItem,
    topic_key: str,
    now_utc: datetime,
) -> bool:
    """Detecta si un evento anunciado previamente acaba de iniciar hoy."""
    if item.kind != "event" or not item.starts_on:
        return False

    started_map = state.get("started_notified", {})
    if item.uid in started_map or topic_key in started_map:
        return False

    start_dt = _parse_iso_date(item.starts_on)
    if not start_dt:
        return False

    today = now_utc.date()
    if not (start_dt <= today <= start_dt + timedelta(days=1)):
        return False

    seen_dt = _parse_iso_dt(state["seen_uids"].get(item.uid)) or _parse_iso_dt(
        state["seen_topics"].get(topic_key)
    )
    return bool(seen_dt and seen_dt.date() < start_dt)


def _mark_seen(state: dict[str, Any], item: AlertItem, topic_key: str, timestamp: str) -> None:
    """Registra UID, tema canónico y estado de arranque si ya inició."""
    state["seen_uids"][item.uid] = timestamp
    state["seen_topics"][topic_key] = timestamp

    now_dt = _parse_iso_dt(timestamp)
    start_dt = _parse_iso_date(item.starts_on)
    if item.is_launch_reminder or (start_dt and now_dt and now_dt.date() >= start_dt):
        started_map = state.setdefault("started_notified", {})
        started_map[item.uid] = timestamp
        started_map[topic_key] = timestamp


def select_pending_alerts(
    state: dict[str, Any],
    items: list[AlertItem],
    *,
    now_utc: datetime | None = None,
    max_alerts: int = MAX_ALERTS_PER_RUN,
    bootstrap_silent: bool = False,
) -> tuple[list[AlertItem], list[AlertItem]]:
    """Separa alertas nuevas + recordatorios de inicio hoy del backlog histórico.

    Devuelve `(to_notify, to_mark_seen_without_notify)`.
    """
    ref_now = now_utc or datetime.now(timezone.utc)
    is_bootstrap = not state.get("initialized", False)

    unseen: list[tuple[AlertItem, str]] = []
    launch_reminders: list[tuple[AlertItem, str]] = []
    seen_batch_topics: set[str] = set()

    for item in items:
        topic_key = build_topic_key(item)
        if topic_key in seen_batch_topics:
            continue
        seen_batch_topics.add(topic_key)

        if not _is_seen(state, item, topic_key, ref_now):
            unseen.append((item, topic_key))
        elif not is_bootstrap and _should_send_launch_reminder(state, item, topic_key, ref_now):
            launch_reminders.append((replace(item, is_launch_reminder=True), topic_key))

    if not is_bootstrap:
        # Prioriza códigos + recordatorios de inicio hoy por encima de anuncios lejanos
        ordered = [
            *(pair[0] for pair in unseen if pair[0].kind == "code"),
            *(pair[0] for pair in launch_reminders),
            *(pair[0] for pair in unseen if pair[0].kind != "code"),
        ]
        return ordered[:max_alerts], []

    if bootstrap_silent:
        return [], [pair[0] for pair in unseen]

    to_notify: list[AlertItem] = []
    to_seed_only: list[AlertItem] = []
    events_per_game: dict[str, int] = {"pogo": 0, "tcgp": 0}

    for item, _ in unseen:
        if len(to_notify) >= max_alerts:
            to_seed_only.append(item)
            continue
        if item.kind == "code":
            to_notify.append(item)
            continue
        if events_per_game.get(item.game, 0) < MAX_BOOTSTRAP_EVENTS_PER_GAME:
            events_per_game[item.game] = events_per_game.get(item.game, 0) + 1
            to_notify.append(item)
        else:
            to_seed_only.append(item)

    return to_notify, to_seed_only


def mark_items_processed(
    state: dict[str, Any],
    items: list[AlertItem],
    *,
    now_utc: datetime | None = None,
) -> None:
    """Marca una lista de alertas como vistas tras enviarse o sembrarse."""
    now_iso = (now_utc or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for item in items:
        _mark_seen(state, item, build_topic_key(item), now_iso)
    state["initialized"] = True


def update_source_health_and_heartbeat(
    state: dict[str, Any],
    source_statuses: list[tuple[str, bool]],
    *,
    had_state_change: bool,
    now_utc: datetime | None = None,
) -> bool:
    """Actualiza contadores de fallo y decide si corresponde commit de heartbeat."""
    now = now_utc or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    failures: dict[str, int] = state.setdefault("source_failures", {})

    for source_name, ok in source_statuses:
        failures[source_name] = 0 if ok else failures.get(source_name, 0) + 1

    last_hb_raw = state.get("last_commit_heartbeat_utc")
    needs_heartbeat = had_state_change or not last_hb_raw
    if not needs_heartbeat and last_hb_raw:
        last_hb = _parse_iso_dt(last_hb_raw)
        if not last_hb or now - last_hb >= timedelta(days=HEARTBEAT_INTERVAL_DAYS):
            needs_heartbeat = True

    if needs_heartbeat:
        state["last_run_utc"] = now_iso
        state["last_commit_heartbeat_utc"] = now_iso

    return needs_heartbeat
