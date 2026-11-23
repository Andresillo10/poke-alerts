"""Generador de explicaciones prácticas, actividades y premios paso a paso en español claro."""

from __future__ import annotations

import re
from typing import Any

from src.glossary import apply_post_translation_fixes
from src.models import AlertItem

_WIN_GOAL_RE = re.compile(r"\b(?:win|gana)\s+(\d+)\s+(?:battles?|batallas?|partidas?|wins?)\b", re.I)
_POINTS_GOAL_RE = re.compile(r"\b(?:get|consigue)\s+(\d+)\s+points?\b", re.I)
_BATTLE_TASK_RE = re.compile(
    r"\b(?:win|gana|participate\s+in|participa\s+en|juega|complete|completa)\s+\d+\s+(?:battles?|batallas?|partidas?)\b",
    re.I,
)
_PACK_TASK_RE = re.compile(r"\b(?:abre|open)\s+\d*\s*(?:sobres?|booster\s+packs?)\b", re.I)
_WONDER_TASK_RE = re.compile(r"\b(?:elecci[oó]n\s+m[aá]gica|elecciones\s+m[aá]gicas|wonder\s+pick)\b", re.I)
_LOGIN_TASK_RE = re.compile(r"\b(?:inicia\s+sesi[oó]n|log\s+in)\b", re.I)


def _extract_goal_phrase(item: AlertItem, milestones: list[str] | None = None) -> str:
    """Detecta la meta numérica real (ej. 'ganar 9 batallas', 'sumar 18 puntos')."""
    hay = " ".join([item.rewards or "", *item.guide_bullets, *(milestones or []), item.summary or ""])
    wins = [int(m.group(1)) for m in _WIN_GOAL_RE.finditer(hay)]
    if wins:
        mx = max(wins)
        return f"ganar {mx} batalla" if mx == 1 else f"ganar {mx} batallas"
    pts = [int(m.group(1)) for m in _POINTS_GOAL_RE.finditer(hay)]
    if pts:
        return f"sumar {max(pts)} puntos"
    return ""


def _norm_milestone_task(line: str) -> str:
    """Normaliza la parte izquierda (tarea) de un hito para deduplicar entre Serebii y PTCGPocket.gg."""
    task = line.split("→", 1)[0].strip().lower()
    task = re.sub(r"partida\(s\)|partidas|batallas|batalla", "batalla", task)
    task = re.sub(r"días|día", "dia", task)
    task = re.sub(r"\s+", " ", task)
    return task


def _is_meaningful_reward(reward_part: str) -> bool:
    """Detecta si el lado derecho del hito describe un premio real (no solo un número suelto '1')."""
    clean = reward_part.strip()
    return bool(clean) and not clean.isdigit()


def extract_event_milestones(
    item: AlertItem,
    *,
    extra_ev: dict[str, Any] | None = None,
    highlights: list[str] | None = None,
) -> list[str]:
    """Extrae una lista limpia y completa de premios/hitos paso a paso (sin truncar a 3 filas)."""
    raw_lines: list[str] = []
    for src in [*(highlights or []), *item.guide_bullets]:
        if src and src not in raw_lines:
            raw_lines.append(src)
    if item.rewards and "Consultar detalle" not in item.rewards and item.rewards not in raw_lines:
        raw_lines.append(item.rewards)

    mission_by_task: dict[str, str] = {}
    mission_order: list[str] = []
    extra_bullets: list[str] = []

    for entry in raw_lines:
        txt = apply_post_translation_fixes(entry)
        low = txt.lower()
        if low.startswith(("misiones:", "missions:")):
            body = txt.split(":", 1)[1].strip()
            for part in body.split(" | "):
                cleaned = apply_post_translation_fixes(part).strip()
                if not cleaned:
                    continue
                # Si la línea mezcla "Cartas y sobres destacados:" al final (caso viejo de rewards)
                if cleaned.lower().startswith(("cartas y sobres destacados:", "featured cards")):
                    extra_bullets.append(f"🃏 {cleaned}")
                    continue
                tkey = _norm_milestone_task(cleaned)
                rew_part = cleaned.split("→", 1)[1].strip() if "→" in cleaned else ""
                if tkey not in mission_by_task:
                    mission_by_task[tkey] = cleaned
                    mission_order.append(tkey)
                else:
                    prev_val = mission_by_task[tkey]
                    prev_rew = prev_val.split("→", 1)[1].strip() if "→" in prev_val else ""
                    # Conserva la versión con nombre de premio real (ej. '1× Mega Mewtwo X (Moneda)' vs '1')
                    if (_is_meaningful_reward(rew_part) and not _is_meaningful_reward(prev_rew)) or (
                        _is_meaningful_reward(rew_part) and len(rew_part) > len(prev_rew)
                    ):
                        mission_by_task[tkey] = cleaned
        elif low.startswith(("bonificaciones:", "bonuses:")):
            body = txt.split(":", 1)[1].strip()
            for b in re.split(r";\s*", body):
                b_clean = apply_post_translation_fixes(b).strip()
                if b_clean and len(b_clean) > 3:
                    extra_bullets.append(f"🔥 {b_clean}")
        elif low.startswith(("variocolor disponibles:", "shinies available:")):
            extra_bullets.append(f"✨ {txt}")
        elif low.startswith(("pokémon salvajes:", "pokémon destacados:", "wild spawns:", "featured pokémon:")):
            extra_bullets.append(f"🌿 {txt}")
        elif low.startswith(("cartas y sobres destacados:", "featured cards")):
            extra_bullets.append(f"🃏 {txt}")
        elif low.startswith("jefes:"):
            extra_bullets.append(f"⚔️ {txt}")

    # Ordena primero hitos de victoria ("Gana 1 batalla", "Gana 3 batallas"...) y luego el resto
    missions = [mission_by_task[k] for k in mission_order]
    win_ms = [m for m in missions if _WIN_GOAL_RE.search(m)]
    other_ms = [m for m in missions if m not in win_ms]

    def _win_sort_key(text: str) -> int:
        m = _WIN_GOAL_RE.search(text)
        return int(m.group(1)) if m else 999

    win_ms.sort(key=_win_sort_key)
    ordered_missions = win_ms + other_ms

    # Deduplica extra_bullets manteniendo orden
    seen_ex: set[str] = set()
    clean_extra: list[str] = []
    for eb in extra_bullets:
        k = re.sub(r"[^a-z0-9]+", "", eb.lower())
        if k and k not in seen_ex:
            seen_ex.add(k)
            clean_extra.append(eb)

    if ordered_missions:
        return (ordered_missions + clean_extra)[:16]
    return clean_extra[:10]


def classify_event_activities(
    item: AlertItem,
    *,
    extra_ev: dict[str, Any] | None = None,
    milestones: list[str] | None = None,
    decks_count: int = 0,
) -> tuple[bool, list[str]]:
    """Determina si el evento exige combates y genera etiquetas claras de qué toca hacer."""
    title_l = item.title.lower()
    ms_text = " ".join(milestones or [])
    hay = " ".join([item.title, item.summary, item.rewards or "", ms_text]).lower()
    ev_type = str((extra_ev or {}).get("eventType") or "").lower()

    if item.kind == "code":
        return False, ["🎁 Canjear código web", "✅ Sin combates"]

    if item.game == "tcgp":
        if item.kind == "news":
            return False, ["📢 Novedad / Expansión", "✅ Sin combates"]

        is_wonder_only = any(k in title_l for k in ("wonder pick", "elección mágica", "mass outbreak", "aparición masiva"))
        title_combat = any(
            k in title_l
            for k in (
                "limited battle",
                "partidas limitadas",
                "combates limitados",
                "drop event",
                "botín",
                "emblem",
                "emblema",
                "ranked",
                "clasificatorio",
                "solo battle",
                "combate individual",
            )
        )
        ms_combat = bool(_BATTLE_TASK_RE.search(ms_text))
        requires_battles = (not is_wonder_only) and (decks_count > 0 or title_combat or ms_combat)

        acts: list[str] = []
        if requires_battles:
            if "limited battle" in hay or "partidas limitadas" in hay or "combate limitado" in hay:
                acts.append("⚔️ Combates Versus (regla limitada)")
            elif "drop event" in hay or "botín" in hay or "solo battle" in hay:
                acts.append("⚔️ Combates Individuales (CPU)")
            elif "ranked" in hay or "clasificatorio" in hay:
                acts.append("🏆 Combates Clasificatorios")
            else:
                acts.append("⚔️ Hacer combates")

        if _PACK_TASK_RE.search(ms_text):
            acts.append("📦 Abrir sobres")
        if is_wonder_only or _WONDER_TASK_RE.search(ms_text):
            acts.append("✨ Elección Mágica")
        if _LOGIN_TASK_RE.search(ms_text) or ("inicia sesión" in hay and not requires_battles):
            acts.append("📅 Iniciar sesión")

        if not requires_battles:
            if not acts:
                acts.append("🎯 Misiones en juego")
            acts.append("✅ Sin combates")
        return requires_battles, list(dict.fromkeys(acts))[:4]

    # Pokémon GO
    if item.kind == "news":
        return False, ["📢 Anuncio oficial"]
    if "duolingo" in hay:
        return False, ["📱 1 lección/día en Duolingo (7 días)", "🎁 Código Investigación Temporal"]
    if "twitch" in hay:
        return False, ["📺 Ver stream (Twitch Drops)", "🎁 Investigación Temporal"]
    if ev_type in ("max-battles", "max-mondays") or any(k in title_l for k in ("combates max", "dynamax", "gigamax", "gigantamax", "lunes max", "max monday")):
        acts = ["🌀 Combates Max en Nodos"]
        if "lunes max" in title_l or "max monday" in title_l:
            acts.append("⏰ Lunes 6:00–7:00 p. m.")
        return True, acts
    if ev_type in ("raid-hour", "raid-day", "raid-battles") or any(k in title_l for k in ("incursiones", "megaincursiones", "raid", "mega raid", "shadow raid")):
        acts = ["⚔️ Incursiones en Gimnasios"]
        if "hora de incursiones" in title_l or "raid hour" in title_l:
            acts.append("⏰ 6:00–7:00 p. m.")
        if "variocolor" in hay or "shiny" in hay:
            acts.append("✨ Posible Shiny")
        return True, acts
    if ev_type == "go-battle-league" or any(k in hay for k in ("liga súper", "liga ultra", "liga master", "great league", "ultra league", "master league", "laic")):
        return True, ["🏆 Combates PvP (Liga GO)"]
    if ev_type == "community-day" or "día de la comunidad" in title_l or "community day" in title_l:
        return False, ["🌿 Capturar salvajes (3 h)", "⚡ Evolucionar por Ataque Legacy"]
    if ev_type == "pokemon-spotlight-hour" or "destacado" in title_l or "spotlight hour" in title_l:
        return False, ["🔦 Capturar 6:00–7:00 p. m.", "✨ Bonus activo 1 hora"]
    if "eclosion" in title_l or "hatch day" in title_l:
        return False, ["🥚 Eclosionar Huevos"]
    if "suerte" in hay or "lucky" in hay:
        return False, ["🤝 Intercambios con Suerte", "🌿 Capturas destacadas"]
    if "go pass" in title_l or "pase" in title_l:
        return False, ["🎟️ Subir niveles del Pase", "🎯 Tareas diarias"]
    return False, ["🎯 Pestaña Hoy / Eventos", "🌿 Capturas y bonus"]


def build_deck_proposal_intro(
    item: AlertItem,
    *,
    decks_count: int,
    decks_updated_label: str = "",
) -> str:
    """Frase clara y directa explicando por qué se proponen mazos con sinergia."""
    if item.game != "tcgp" or decks_count <= 0:
        return ""
    hay = " ".join([item.title, item.summary]).lower()
    rule_hint = ""
    if "limited battle" in hay or "partidas limitadas" in hay or "fusión dual" in hay:
        rule_hint = " respetando la regla limitada (solo Fusión Dual C1 / Promo-C)"
    elif "drop event" in hay or "botín" in hay or "solo battle" in hay:
        rule_hint = " para farmear rápido en dificultad Experto"
    elif "ranked" in hay or "clasificatorio" in hay:
        rule_hint = " tomados del meta real de torneos"
    upd_txt = f" • 🔄 Revisado/actualizado: {decks_updated_label}" if decks_updated_label else ""
    return (
        f"Mira, como este evento requiere combates, te propongo estos {decks_count} mazos "
        f"optimizados por sinergia{rule_hint} para cumplirlo (sigo revisando si aparece uno mejor).{upd_txt}"
    )


def build_event_action_guide(
    item: AlertItem,
    *,
    extra_ev: dict[str, Any] | None = None,
    decks_count: int = 0,
    pogo_cards_count: int = 0,
    milestones: list[str] | None = None,
    requires_battles: bool | None = None,
    compact: bool = False,
) -> str:
    """Explica en español sencillo de qué trata el evento y exactamente qué toca hacer."""
    title_l = item.title.lower()
    ms_list = milestones if milestones is not None else extract_event_milestones(item, extra_ev=extra_ev)
    if requires_battles is None:
        requires_battles, _ = classify_event_activities(
            item, extra_ev=extra_ev, milestones=ms_list, decks_count=decks_count
        )
    ms_text = " ".join(ms_list).lower()
    hay = " ".join([item.title, item.summary, item.rewards or "", *item.guide_bullets, ms_text]).lower()
    ev_type = str((extra_ev or {}).get("eventType") or "").lower()
    goal = _extract_goal_phrase(item, ms_list)

    if item.kind == "code":
        site = "store.pokemongo.com" if item.game == "pogo" else "gift.pokemontcgpocket.com"
        return (
            f"No toca combatir: copia el código, entra a {site} con tu cuenta vinculada y canjéalo antes de que venza."
            if compact
            else f"No toca combatir. Copia el código de regalo, abre {site} con tu cuenta vinculada, pégalo y reclama la recompensa en tu buzón del juego."
        )

    if item.game == "tcgp":
        if "limited battle" in hay or "partidas limitadas" in hay or "combate limitado" in hay or "limitado" in hay or "limitada" in hay:
            meta_txt = f"toca {goal}" if goal else "toca jugar y ganar combates"
            extra_tasks: list[str] = []
            if _LOGIN_TASK_RE.search(ms_text):
                extra_tasks.append("iniciar sesión")
            if _PACK_TASK_RE.search(ms_text):
                extra_tasks.append("abrir sobres con energía")
            if _WONDER_TASK_RE.search(ms_text):
                extra_tasks.append("hacer Elecciones Mágicas")
            daily_txt = f", además de {', '.join(extra_tasks)}" if extra_tasks else ""
            if compact:
                return f"SÍ toca combatir en Partidas ➔ Versus ➔ Encuentro de evento (solo cartas de Fusión Dual C1/Promo-C): {meta_txt}{daily_txt}."
            return (
                f"SÍ toca hacer combates (con restricción). Entra a Partidas ➔ Versus ➔ Encuentro de evento usando solo cartas de Fusión Dual (C1) y Promo-C Vol. 1: "
                f"{meta_txt} para llevarte todos los premios{daily_txt}. Abajo te propongo {decks_count or 3} mazos con sinergia listos para importar por QR."
            )

        if "emblem" in hay or "emblema" in hay:
            meta_txt = f"hasta {goal}" if goal else "hasta completar los hitos de victorias"
            if compact:
                return f"SÍ toca combatir en Partidas ➔ Versus ➔ Encuentro de evento {meta_txt} usando los mazos propuestos."
            return (
                f"SÍ toca hacer combates contra otros jugadores. Entra a Partidas ➔ Versus ➔ Encuentro de evento y juega {meta_txt} "
                f"para desbloquear emblemas y Relojes de Arena. Abajo te propongo {decks_count or 3} mazos con sinergia."
            )

        if "drop event" in hay or "botín" in hay or "solo battle" in hay or "combate individual" in hay:
            if compact:
                return "SÍ toca combatir contra la CPU en Partidas ➔ Individual ➔ Evento de Botín; farmea dificultad Experto (si pierdes no gastas energía)."
            return (
                "SÍ toca hacer combates contra la máquina (CPU). Entra a Partidas ➔ Individual ➔ Evento de Botín y sube hasta dificultad Experto: "
                "si pierdes o te rindes NO gastas energía de evento, así que repite Experto con los mazos recomendados para farmear Sobres Promo."
            )

        if "wonder pick" in hay or "elección mágica" in hay or "mass outbreak" in hay or "aparición masiva" in hay:
            if compact:
                return "NO toca combatir: entra al menú Elección Mágica varias veces al día (rotan cada ~30 min a 1 h) para sacar las cartas promo."
            return (
                "NO toca hacer combates. Entra al menú Elección Mágica varias veces al día: las elecciones extra gratuitas y especiales (Chansey Pick) "
                "rotan cada ~30 min a 1 hora para sacar las cartas destacadas y cupones de tienda."
            )

        if "ranked" in hay or "clasificatorio" in hay:
            if compact:
                return "SÍ toca combatir en Partidas ➔ Versus ➔ Encuentro clasificatorio para subir de rango y cobrar Relojes al cierre."
            return (
                "SÍ toca hacer combates competitivos. Entra a Partidas ➔ Versus ➔ Encuentro clasificatorio y usa los mazos del meta con mejor sinergia "
                "para subir de liga y cobrar Relojes de Arena e insignia al terminar la temporada."
            )

        if item.kind == "news":
            return (
                "Anuncio oficial de expansión o actualización (sin combates): revisa las novedades y ahorra tus Relojes de Arena."
                if compact
                else "Anuncio informativo oficial de Pokémon TCG Pocket (no requiere combates). Revisa qué cartas o cambios llegan para planificar tus Relojes de Arena."
            )

        # Evento de misiones TCG Pocket: distingue explícitamente si exige combates o solo login/sobres
        if not requires_battles:
            tasks_es: list[str] = []
            if _LOGIN_TASK_RE.search(ms_text) or "inicia sesión" in hay:
                tasks_es.append("iniciar sesión cada día")
            if _PACK_TASK_RE.search(ms_text):
                tasks_es.append("abrir sobres con tu energía gratis")
            if _WONDER_TASK_RE.search(ms_text):
                tasks_es.append("hacer Elecciones Mágicas")
            todo_txt = " y ".join(tasks_es) if tasks_es else "reclamar las tareas diarias en Inicio ➔ Misiones"
            if compact:
                return f"NO toca combatir: solo debes {todo_txt} en Inicio ➔ Misiones para reclamar gratis todos los sobres/premios."
            return (
                f"NO toca hacer combates (no necesitas armar mazo). Solo entra en Inicio ➔ Misiones y dedícate a {todo_txt} "
                f"para llevarte gratis todos los premios listados abajo antes del cierre."
            )

        extra_goal = f" ({goal})" if goal else ""
        if compact:
            return f"SÍ incluye combates{extra_goal}: revisa Inicio ➔ Misiones y usa los mazos recomendados abajo para completarlo rápido."
        return (
            f"Evento de misiones que SÍ incluye combates{extra_goal}. Entra a Inicio ➔ Misiones para revisar cada objetivo "
            f"y usa cualquiera de los {decks_count or 3} mazos propuestos abajo."
        )

    # ======================== POKÉMON GO ========================
    if "duolingo" in hay:
        return (
            "Completa 1 lección diaria en la app Duolingo durante 7 días seguidos para recibir un código de Investigación Temporal en Pokémon GO."
            if compact
            else "Qué tienes que hacer: abre la app Duolingo y completa al menos 1 lección al día durante 7 días seguidos. Al cumplir la racha recibirás un código promocional para desbloquear esta Investigación Temporal y Pokémon temáticos en Pokémon GO."
        )

    if "twitch" in hay:
        return (
            "Mira la transmisión oficial con tu cuenta vinculada en Twitch Drops para recibir el código de Investigación Temporal."
            if compact
            else "Qué tienes que hacer: vincula tu cuenta de Pokémon Trainer Club con Twitch, mira la transmisión oficial el tiempo requerido para activar el Twitch Drop y canjea el código de la Investigación Temporal antes del cierre."
        )

    if ev_type == "go-battle-league" or any(
        k in hay for k in ("liga súper", "liga ultra", "liga master", "great league", "ultra league", "master league", "laic", "campeonato internacional")
    ):
        if compact:
            return "Combates PvP en Menú ➔ Combates: arma equipo de 3 dentro del límite de PC usando el Top PvPoke de abajo."
        return (
            "Qué tienes que hacer: entra a Menú ➔ Combates (Liga Combates GO) y compite contra otros entrenadores con un equipo de 3 Pokémon "
            "dentro del límite de PC de la liga activa. Revisa abajo el ranking Top PvPoke y sus ataques ideales."
        )

    if ev_type == "pokemon-spotlight-hour" or "destacado" in title_l or "spotlight hour" in title_l:
        if compact:
            return "Solo hoy de 6:00 a 7:00 p. m.: sal a capturar con Incienso, aprovecha el bonus de 1 hora y filtra 100% IV/PvP."
        return (
            "Qué tienes que hacer: este evento dura únicamente 1 hora (6:00 p. m. a 7:00 p. m. hora local). Sal a capturar al Pokémon destacado "
            "con Incienso (y Trozo Estrella o Huevo Suerte según el bonus) y luego pega el filtro de caja de abajo para detectar rápido los 100% IV o PvP."
        )

    if ev_type == "community-day" or "día de la comunidad" in title_l or "días de la comunidad" in title_l or "community day" in title_l:
        if compact:
            return "Sal a capturar en la franja de 3 h con Mega Nv.3 (+Caramelos XL) y evoluciona hasta 5 h después para el Ataque Legacy."
        return (
            "Qué tienes que hacer: sal a capturar en estado salvaje durante las 3 horas del evento con una Mega Evolución Nv.3 del mismo tipo activa "
            "para multiplicar Caramelos XL y buscar Shiny. Muy importante: evoluciona antes de 5 horas tras el cierre para obtener el Ataque Exclusivo (Legacy)."
        )

    if ev_type in ("max-battles", "max-mondays") or any(k in title_l for k in ("combates max", "dynamax", "gigamax", "gigantamax", "max battle", "lunes max", "max monday")):
        hour_note = " (con hora destacada el lunes de 6:00 a 7:00 p. m. hora local)" if ("lunes max" in title_l or "max monday" in title_l) else ""
        if compact:
            return f"Ve a Nodos Energéticos{hour_note}, recoge Partículas Max y vence al jefe en Combates Max con tus Dinamax/Gigamax."
        return (
            f"Qué tienes que hacer: camina hacia los Nodos Energéticos (torres moradas) en el mapa{hour_note}, recoge tu cupo diario de Partículas Max "
            f"y vence al jefe en Combates Max usando Pokémon Dinamax/Gigamax con ventaja de tipo (prioriza subir el Ataque Max). Revisa abajo el PC 100% IV y counters."
        )

    if ev_type in ("raid-hour", "raid-day", "raid-battles") or any(
        k in title_l for k in ("incursiones", "megaincursiones", "raid hour", "raid day", "raid battles", "mega raid", "shadow raid")
    ):
        hour_note = " (foco especial de 6:00 a 7:00 p. m. hora local)" if ("hora de incursiones" in title_l or "raid hour" in title_l) else ""
        if compact:
            return f"Ve a Gimnasios (o pase remoto){hour_note} y vence al jefe con los Top Counters; revisa si salió 100% IV con el PC de abajo."
        return (
            f"Qué tienes que hacer: busca este jefe en Gimnasios cercanos (o entra con pase remoto){hour_note}, derrótalo usando los Top Counters "
            f"listados abajo y activa una Mega del mismo tipo del jefe para ganar Caramelos XL extra al atraparlo. Compara el PC de captura para saber si es 100% IV."
        )

    if "eclosion" in title_l or "hatch day" in title_l:
        if compact:
            return "Vacía huevos viejos antes de empezar, incuba huevos del evento con bonus de distancia y abre en lote con Trozo Estrella."
        return (
            "Qué tienes que hacer: vacía tus huevos antiguos antes de que inicie el evento, gira Poképaradas para llenar huevos del evento mientras "
            "esté activa la reducción de distancia y activa un Trozo Estrella justo antes de abrir varios a la vez."
        )

    if "go pass" in title_l or "pase" in title_l:
        return (
            "Entra a la pestaña Eventos ➔ GO Pass y completa tareas diarias de captura/incursiones para subir niveles y cobrar recompensas."
            if compact
            else "Qué tienes que hacer: abre la pestaña Eventos ➔ GO Pass dentro de Pokémon GO y cumple los objetivos diarios (capturar, girar paradas, incursiones) para subir de rango y desbloquear las recompensas antes de fin de mes."
        )

    if "suerte" in hay or "lucky" in hay:
        if compact:
            return "Captura Pokémon destacados y haz intercambios diarios con amigos para aprovechar el bonus de Amistad/Intercambio con Suerte."
        return (
            "Qué tienes que hacer: captura los Pokémon destacados del evento y realiza intercambios diarios con tus amigos para aprovechar el aumento "
            "de probabilidad de Amistad con Suerte e Intercambios con Suerte."
        )

    if item.kind == "news":
        clean_sum = apply_post_translation_fixes(item.summary or "").strip()
        if clean_sum and len(clean_sum) >= 35 and clean_sum.lower() != title_l:
            short = clean_sum if len(clean_sum) <= 220 else clean_sum[:217].rsplit(" ", 1)[0] + "..."
            return short
        return (
            "Anuncio oficial de Pokémon GO: revisa fechas y recompensas próximas en el enlace oficial."
            if compact
            else "Anuncio informativo de Pokémon GO. Revisa las fechas clave y posibles códigos o investigaciones temporales asociadas."
        )

    # Si el resumen real scraped (ej. LeekDuck/Serebii) explica de qué trata, úsalo como base clara
    clean_sum = apply_post_translation_fixes(item.summary or "").strip()
    if clean_sum and len(clean_sum) >= 40 and clean_sum.lower() != title_l and "pokemon go" not in clean_sum.lower()[:20]:
        short = clean_sum if len(clean_sum) <= 230 else clean_sum[:227].rsplit(" ", 1)[0] + "..."
        return f"De qué trata: {short} Revisa la pestaña Hoy/Eventos del juego para reclamar tareas y bonificaciones."

    return (
        "Abre la pestaña Hoy/Eventos en Pokémon GO, aprovecha los bonus activos y captura los Pokémon destacados antes del cierre."
        if compact
        else "Qué tienes que hacer: abre la pestaña Eventos / Hoy dentro de Pokémon GO para seguir las tareas temporales, aprovecha las bonificaciones activas en el mapa y usa el filtro de caja de abajo para valorar tus capturas."
    )


def build_search_aliases(item: AlertItem, *, title_es: str = "") -> str:
    """Construye sinónimos ES/EN comunes para que el buscador encuentre eventos pese a variaciones o typos."""
    base = " ".join([item.title, title_es, item.category if hasattr(item, "category") else "", item.summary]).lower()
    aliases: list[str] = []
    if "dual fusion" in base or "fusión dual" in base:
        aliases.extend(["Fusión Dual", "Dual Fusion", "C1"])
    if "limited battle" in base or "partidas limitadas" in base or "combates limitados" in base:
        aliases.extend(["Partidas limitadas", "Partidas ilimitadas", "Combates limitados", "Versus", "Encuentro de evento"])
    if "wonder pick" in base or "elección mágica" in base:
        aliases.extend(["Elección Mágica", "Wonder Pick", "Chansey Pick", "Gratis"])
    if "drop event" in base or "botín" in base:
        aliases.extend(["Evento de Botín", "Drop Event", "Combate Individual", "Sobres Promo"])
    if "emblem" in base or "emblema" in base:
        aliases.extend(["Evento de Emblemas", "Emblem Event", "Versus"])
    if "raid" in base or "incursiones" in base:
        aliases.extend(["Incursiones", "Raids", "Hora Legendaria", "Gimnasios", "Counters", "Hundo"])
    if "community day" in base or "día de la comunidad" in base:
        aliases.extend(["Día de la Comunidad", "Community Day", "Legacy", "Caramelos XL"])
    if "spotlight" in base or "destacado" in base:
        aliases.extend(["Hora Destacada", "Spotlight Hour", "6pm"])
    if "dynamax" in base or "gigamax" in base or "max" in base:
        aliases.extend(["Combates Max", "Dinamax", "Gigamax", "Partículas Max", "Nodos"])
    return " ".join(dict.fromkeys(aliases))
