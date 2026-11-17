"""Persistencia de estado, deduplicación por tema y control anti-spam en arranque."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _prune_dict(mapping: dict[str, str], limit: int) -> dict[str, str]:
    """Mantiene únicamente las últimas `limit` entradas de un mapeo cronológico."""
    if len(mapping) <= limit:
        return mapping
    items = list(mapping.items())[-limit:]
    return dict(items)


def _is_seen(
    state: dict[str, Any],
    item: AlertItem,
    topic_key: str,
    now_utc: datetime,
) -> bool:
    """Verifica si el UID ya se envió o si el mismo tema salió dentro de la ventana TTL."""
    if item.uid in state["seen_uids"]:
        return True

    topic_ts_raw = state["seen_topics"].get(topic_key)
    if not topic_ts_raw:
        return False
    try:
        topic_ts = datetime.fromisoformat(topic_ts_raw.replace("Z", "+00:00"))
        if topic_ts.tzinfo is None:
            topic_ts = topic_ts.replace(tzinfo=timezone.utc)
        return (now_utc - topic_ts) < timedelta(days=TOPIC_DEDUP_TTL_DAYS)
    except ValueError:
        return True


def _mark_seen(state: dict[str, Any], item: AlertItem, topic_key: str, timestamp: str) -> None:
    """Registra UID y tema canónico como procesados."""
    state["seen_uids"][item.uid] = timestamp
    state["seen_topics"][topic_key] = timestamp


def select_pending_alerts(
    state: dict[str, Any],
    items: list[AlertItem],
    *,
    now_utc: datetime | None = None,
    max_alerts: int = MAX_ALERTS_PER_RUN,
    bootstrap_silent: bool = False,
) -> tuple[list[AlertItem], list[AlertItem]]:
    """Separa alertas nuevas a enviar del backlog histórico en arranque inicial.

    Devuelve `(to_notify, to_mark_seen_without_notify)`.
    """
    ref_now = now_utc or datetime.now(timezone.utc)
    is_bootstrap = not state.get("initialized", False)

    unseen: list[tuple[AlertItem, str]] = []
    seen_batch_topics: set[str] = set()
    for item in items:
        topic_key = build_topic_key(item)
        if _is_seen(state, item, topic_key, ref_now) or topic_key in seen_batch_topics:
            continue
        seen_batch_topics.add(topic_key)
        unseen.append((item, topic_key))

    if not is_bootstrap:
        chosen = [pair[0] for pair in unseen[:max_alerts]]
        return chosen, []

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
        try:
            last_hb = datetime.fromisoformat(last_hb_raw.replace("Z", "+00:00"))
            if now - last_hb >= timedelta(days=HEARTBEAT_INTERVAL_DAYS):
                needs_heartbeat = True
        except ValueError:
            needs_heartbeat = True

    if needs_heartbeat:
        state["last_run_utc"] = now_iso
        state["last_commit_heartbeat_utc"] = now_iso

    return needs_heartbeat
