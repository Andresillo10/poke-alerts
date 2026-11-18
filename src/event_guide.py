"""Generador de explicaciones prácticas en español claro para Telegram y el Dashboard."""

from __future__ import annotations

import re
from typing import Any

from src.models import AlertItem

_WIN_GOAL_RE = re.compile(r"\b(?:win|gana)\s+(\d+)\s+(?:battles?|partidas?|wins?)\b", re.I)
_POINTS_GOAL_RE = re.compile(r"\b(?:get|consigue)\s+(\d+)\s+points?\b", re.I)


def _extract_goal_phrase(item: AlertItem) -> str:
    """Detecta la meta numérica real (ej. '9 victorias', '18 puntos') si aparece en el evento."""
    hay = " ".join([item.rewards or "", *item.guide_bullets, item.summary or ""])
    wins = [int(m.group(1)) for m in _WIN_GOAL_RE.finditer(hay)]
    if wins:
        return f"ganar {max(wins)} partidas"
    pts = [int(m.group(1)) for m in _POINTS_GOAL_RE.finditer(hay)]
    if pts:
        return f"sumar {max(pts)} puntos"
    return ""


def build_event_action_guide(
    item: AlertItem,
    *,
    extra_ev: dict[str, Any] | None = None,
    decks_count: int = 0,
    pogo_cards_count: int = 0,
    compact: bool = False,
) -> str:
    """Explica de forma sencilla dónde entrar y qué hacer para completar el evento."""
    title_l = item.title.lower()
    hay = " ".join([item.title, item.summary, item.rewards or "", *item.guide_bullets]).lower()
    ev_type = str((extra_ev or {}).get("eventType") or "").lower()
    goal = _extract_goal_phrase(item)

    if item.kind == "code":
        site = "store.pokemongo.com" if item.game == "pogo" else "gift.pokemontcgpocket.com"
        return (
            f"Copia el código, entra a {site} con tu cuenta vinculada y canjéalo antes de su vencimiento."
            if compact
            else f"📍 Cómo canjearlo: copia el código de regalo, abre {site} con tu cuenta del juego vinculada, pégalo y reclama la recompensa directo en tu buzón del juego."
        )

    if item.game == "tcgp":
        if "limited battle" in hay or "partidas limitadas" in hay or "combate limitado" in hay or "limitado" in hay:
            meta_txt = f"debes {goal} en Versus" if goal else "combate y gana partidas en Versus"
            if compact:
                return f"Entra a Partidas ➔ Versus ➔ Encuentro de evento; usa solo cartas permitidas de Fusión Dual/Promo C y {meta_txt}."
            return (
                f"📍 Dónde jugar: Partidas ➔ Versus ➔ Encuentro de evento. "
                f"🎯 Cómo completarlo: arma tu mazo usando solo cartas permitidas por la regla (expansión Fusión Dual C1 y Promo-C Vol. 1; si tu baraja marca «No disponible», importa cualquiera de los {decks_count or 3} QR de abajo), {meta_txt} y completa las misiones diarias de abrir sobres e iniciar sesión."
            )

        if "emblem" in hay or "emblema" in hay:
            meta_txt = f"y consigue {goal}" if goal else "hasta reunir las victorias de cada emblema"
            if compact:
                return f"Entra a Partidas ➔ Versus ➔ Encuentro de evento y combate con un mazo recomendado {meta_txt}."
            return (
                f"📍 Dónde jugar: Partidas ➔ Versus ➔ Encuentro de evento. "
                f"🎯 Cómo completarlo: entra con un mazo meta recomendado, juega partidas contra otros entrenadores {meta_txt} para desbloquear los emblemas del perfil y Relojes de Arena."
            )

        if "drop event" in hay or "botín" in hay or "solo battle" in hay or "combate individual" in hay:
            if compact:
                return "Entra a Partidas ➔ Individual; supera dificultades hasta Experto (la energía solo se gasta al ganar) para cobrar Sobres Promo."
            return (
                "📍 Dónde jugar: Partidas ➔ Individual ➔ Evento de Botín. "
                "🎯 Cómo completarlo: combate contra la CPU subiendo de dificultad hasta Experto. Si pierdes o te rindes no gastas energía de evento (recarga 1 cada 12 h); repite Experto para asegurar Sobres Promo y misiones."
            )

        if "wonder pick" in hay or "elección mágica" in hay or "mass outbreak" in hay or "aparición masiva" in hay:
            if compact:
                return "Revisa Elección Mágica seguido: las elecciones extra/Chansey rotan cada 30 min–1 h aprox.; busca las cartas destacadas del evento."
            return (
                "📍 Dónde entrar: menú Elección Mágica. "
                "🎯 Cómo completarlo: las elecciones extra gratuitas y especiales (Chansey Pick) aparecen y cambian cada ~30 min a 1 hora; entra varias veces al día para sacar las cartas promo sin desperdiciar relojes y canjea cupones en la Tienda."
            )

        if "ranked" in hay or "clasificatorio" in hay:
            if compact:
                return "Entra a Partidas ➔ Versus ➔ Encuentro clasificatorio y sube de rango con mazos del meta semanal antes del cierre."
            return (
                "📍 Dónde jugar: Partidas ➔ Versus ➔ Encuentro clasificatorio. "
                "🎯 Cómo aprovecharlo: compite con los mazos más consistentes del meta para subir de liga durante la temporada; al finalizar recibirás Relojes de Arena e insignia oficial según tu rango máximo."
            )

        if item.kind == "news":
            return (
                "Revisa el anuncio oficial de expansión/actualización y prepara tus Relojes de Arena para nuevos sobres."
                if compact
                else "📍 Qué implica: novedad o próxima actualización oficial de Pokémon TCG Pocket. Revisa las cartas y fechas clave para planificar tus Relojes de Arena de Sobres."
            )

        extra_goal = f" (meta: {goal})" if goal else ""
        if compact:
            return f"Cumple las misiones en Inicio ➔ Misiones (iniciar sesión, abrir sobres o combates{extra_goal}) antes del cierre."
        return (
            f"📍 Dónde entrar: pantalla de Inicio ➔ Misiones. "
            f"🎯 Cómo completarlo: entra diariamente para cobrar ingresos, abre sobres con tu energía gratuita o realiza los combates pedidos{extra_goal} antes de que venza el evento."
        )

    # Pokémon GO
    if ev_type == "go-battle-league" or any(
        k in hay for k in ("liga súper", "liga ultra", "liga master", "great league", "ultra league", "master league", "laic", "campeonato internacional")
    ):
        if compact:
            return "Entra a Menú ➔ Combates; usa el Top PvPoke e IVs recomendados para la liga activa antes del recambio."
        return (
            "📍 Dónde jugar: Menú principal ➔ Combates (Liga Combates GO). "
            "🎯 Cómo completarlo: arma un equipo de 3 Pokémon dentro del límite de PC de la liga activa siguiendo el ranking Top 6 de PvPoke e IVs recomendados abajo."
        )

    if ev_type == "pokemon-spotlight-hour" or "destacado" in title_l or "spotlight hour" in title_l:
        if compact:
            return "Juega de 6:00 a 7:00 p. m. hora local con Incienso y aplica el filtro de caja para revisar 100% IV y PvP."
        return (
            "📍 Cuándo jugar: únicamente de 6:00 p. m. a 7:00 p. m. hora local. "
            "🎯 Cómo aprovecharlo: camina con Incienso (y Trozo Estrella o Huevo Suerte si el bonus es Polvo o PX); al terminar copia el filtro de caja de abajo para detectar rápido Pokémon 100% IV o buenos en PvP."
        )

    if ev_type == "community-day" or "día de la comunidad" in title_l or "días de la comunidad" in title_l or "community day" in title_l:
        if compact:
            return "Captura durante las 3 h con Mega Nv.3 del mismo tipo (+XL) y evoluciona hasta 5 h después para el Ataque Legacy."
        return (
            "📍 Cómo completarlo: sal a capturar en la franja de 3 horas con una Mega Evolución Nv.3 del mismo tipo activa para multiplicar Caramelos XL. Recuerda evolucionar hasta 5 horas después del cierre para aprender el Ataque Exclusivo (Legacy)."
        )

    if ev_type in ("max-battles", "max-mondays") or any(k in title_l for k in ("combates max", "dynamax", "gigamax", "gigantamax", "max battle", "lunes max", "max monday")):
        if compact:
            return "Junta Partículas Max en Nodos Energéticos y vence jefes con equipo Dinamax/Gigamax con ventaja elemental."
        return (
            "📍 Dónde jugar: Nodos Energéticos en el mapa. "
            "🎯 Cómo completarlo: recoge tu límite diario de Partículas Max en los nodos, combate en grupo usando Dinamax/Gigamax con ventaja de tipo (sube Ataque Max primero) y cobra las misiones temporales."
        )

    if ev_type in ("raid-hour", "raid-day", "raid-battles") or any(
        k in title_l for k in ("incursiones", "megaincursiones", "raid hour", "raid day", "raid battles", "mega raid", "shadow raid")
    ):
        if compact:
            return "Vence al jefe en Gimnasios con los Top Counters (×1.6/×2.56 daño), usa Mega del mismo tipo y compara el PC 100%."
        return (
            "📍 Dónde jugar: Gimnasios (o pases remotos). "
            "🎯 Cómo completarlo: arma tu equipo con los Top Counters mostrados abajo, activa una Mega del mismo tipo del jefe para sumar Caramelos XL al capturar y verifica si salió 100% IV comparando con el PC normal o potenciado por clima."
        )

    if "eclosion" in title_l or "hatch day" in title_l:
        if compact:
            return "Vacía huevos viejos antes de iniciar, usa Superincubadoras cuando el bonus esté activo y abre en lote con Trozo Estrella."
        return (
            "📍 Cómo completarlo: vacía tus huevos antiguos antes de la hora de inicio, coloca huevos del evento cuando comience la reducción de distancia y usa Trozo Estrella justo antes de abrir una tanda de 9 huevos."
        )

    if "suerte" in hay or "lucky" in hay:
        if compact:
            return "Prioriza capturas shiny/destacadas y aprovecha la probabilidad aumentada de Intercambio con Suerte y Amistad con Suerte."
        return (
            "📍 Cómo aprovecharlo: busca los Pokémon destacados / Variocolor en estado salvaje e incursiones, y coordina intercambios diarios con amigos para aprovechar el aumento en Amistad con Suerte e Intercambios con Suerte."
        )

    if pogo_cards_count > 0 and ("minior" in hay or "kecleon" in hay or "spawns" in hay or "salvajes" in hay):
        if compact:
            return "Captura en estado salvaje en el horario del evento, aprovecha los bonus y revisa IVs con el filtro de caja abajo."
        return (
            "📍 Cómo aprovecharlo: explora y captura en el mapa dentro del horario activo (revisando Poképaradas si aparece Kecleon), activa los bonus señalados abajo y pega el filtro rápido en tu bolsa Pokémon para valorar 100% IV y PvP."
        )

    if item.kind == "news":
        return (
            "Revisa la novedad oficial de Pokémon GO para anticipar fechas, investigaciones o recompensas de torneo."
            if compact
            else "📍 Qué hacer: revisa las fechas y enlaces del anuncio oficial en Pokémon GO; si incluye recompensas por stream o investigaciones temporales, reclámalas en la pestaña Eventos/Hoy antes del cierre."
        )

    return (
        "Revisa la pestaña Hoy/Eventos del juego, aprovecha los bonus activos y completa tareas temporales antes del cierre."
        if compact
        else "📍 Cómo completarlo: abre la pestaña Eventos / Hoy dentro de Pokémon GO para ver el progreso de misiones o del Pase, aprovecha las bonificaciones activas (suerte, Polvo, regalos o capturas) y usa el filtro de caja abajo antes del fin del evento."
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
