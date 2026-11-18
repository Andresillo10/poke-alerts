"""Pruebas unitarias deterministas (sin red) para poke-alerts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from PIL import Image

from src.card_renderer import render_alert_card
from src.catalog_store import load_events_history, sync_events_catalog
from src.code_digest import (
    build_active_code_entries,
    build_weekly_codes_caption,
    build_weekly_codes_keyboard,
    render_weekly_codes_card,
)
from src.deck_qr import (
    TRAINER_ID_OFFSET,
    CardCatalog,
    EventDeckConstraints,
    build_qr_download_url,
    build_three_decks_for_event,
    decode_deck_payload,
    describe_deck_strategy,
    encode_deck_payload,
    extract_event_constraints,
    validate_deck,
)
from src.deck_sheet_renderer import render_single_deck_card
from src.enricher import (
    parse_ptcgpocket_detail_html,
    parse_scrapedduck_extra,
    parse_serebii_detail_html,
    select_best_deck_post,
)
from src.main import _select_preview_sample
from src.models import AlertItem, build_topic_key
from src.pogo_intel import (
    PogoCatalog,
    build_feature_pogo_card,
    build_pogo_event_cards,
    build_pogo_weekly_meta_report,
    build_raid_card,
    calc_cp,
    compute_weaknesses,
)
from src.pogo_sheet_renderer import render_pogo_tactical_card
from src.scrapers import (
    parse_leekduck_promos_html,
    parse_ptcgpocket_codes_html,
    parse_ptcgpocket_wp_posts,
    parse_serebii_end_date,
    parse_serebii_start_date,
)
from src.state import (
    default_state,
    mark_items_processed,
    select_pending_alerts,
    should_send_one_time_ping,
    should_send_weekly_codes,
    should_send_weekly_meta,
    update_source_health_and_heartbeat,
)
from src.telegram_client import (
    build_inline_keyboard,
    build_pogo_tactical_caption,
    build_pogo_tactical_keyboard,
    build_single_deck_caption,
    build_single_deck_keyboard,
    build_telegram_caption,
)
from src.translator import localize_alert_item, translate_en_to_es
from src.weekly_meta import (
    WeeklyArchetypeSummary,
    WeeklyMetaReport,
    parse_completed_tournaments_html,
    parse_limitless_decklist_cards,
    parse_tournament_standings_rows,
    pick_champion_deck_for_slot,
)


class CorePipelineTests(unittest.TestCase):
    """Valida reglas de negocio, deduplicación, traducción, renderizado y Telegram."""

    def test_topic_dedup_across_sources_and_distinct_months(self) -> None:
        item_a = AlertItem(
            uid="serebii-tcgp:dual-fusion",
            game="tcgp",
            kind="event",
            title="Dual Fusion Emblem Event",
            summary="Win battles",
            url="https://www.serebii.net/tcgpocket/events.shtml",
            source_name="Serebii TCGP",
        )
        item_b = AlertItem(
            uid="ptcgpocket-gg:dual-fusion",
            game="tcgp",
            kind="event",
            title="Dual Fusion Emblem Event Guide",
            summary="Deck guide",
            url="https://ptcgpocket.gg/dual-fusion-emblem-event/",
            source_name="PTCGPocket.gg",
        )
        item_oct = AlertItem(
            uid="serebii-tcgp:oct",
            game="tcgp",
            kind="event",
            title="Community Week Missions October 2026",
            summary="Missions",
            url="https://www.serebii.net/tcgpocket/events.shtml",
            source_name="Serebii TCGP",
        )
        item_nov = AlertItem(
            uid="serebii-tcgp:nov",
            game="tcgp",
            kind="event",
            title="Community Week Missions November 2026",
            summary="Missions",
            url="https://www.serebii.net/tcgpocket/events.shtml",
            source_name="Serebii TCGP",
        )
        self.assertEqual(build_topic_key(item_a), build_topic_key(item_b))
        self.assertNotEqual(build_topic_key(item_oct), build_topic_key(item_nov))

    def test_promo_code_and_glossary_preserved_locally(self) -> None:
        text = "Wonder Pick Event with code ADIDASxPOKEMON, Max Mushroom and Pack Hourglasses!"
        translated = translate_en_to_es(
            text,
            protected_tokens=["ADIDASxPOKEMON"],
            enable_remote=False,
        )
        self.assertIn("Evento de Elección Mágica", translated)
        self.assertIn("Maxiseta", translated)
        self.assertIn("ADIDASxPOKEMON", translated)
        self.assertIn("Relojes de Arena de Sobres", translated)

    def test_leekduck_parser_skips_expired_cards(self) -> None:
        html = """
        <div class="promo-card">
          <h3 class="title">Active Shoe</h3>
          <div class="description"><p>Get item</p></div>
          <div class="code-display"><p class="text">LIVE123</p></div>
          <span class="expiry" data-expires="2027-01-15 23:59:59 -0800">Expires: Jan 15, 2027</span>
        </div>
        <div class="promo-card expired">
          <h3 class="title">Old Item</h3>
          <div class="code-display"><p class="text">OLD999</p></div>
          <span class="expiry" data-expires="2026-11-01 23:59:59 -0800">Expired</span>
        </div>
        <div class="promo-card">
          <h3 class="title">Past Date Without Class</h3>
          <div class="code-display"><p class="text">PAST001</p></div>
          <span class="expiry" data-expires="2026-09-30 23:59:59 -0700">Expires: Sep 30, 2026</span>
        </div>
        """
        now = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
        res = parse_leekduck_promos_html(html, now_utc=now)
        self.assertTrue(res.ok)
        self.assertEqual([item.promo_code for item in res.items], ["LIVE123"])

    def test_spanish_item_skips_translation(self) -> None:
        item = AlertItem(
            uid="pogolive:es",
            game="pogo",
            kind="news",
            title="Día de la Comunidad",
            summary="Texto ya en español",
            url="https://pokemongolive.com/es-MX/news",
            source_name="Pokémon GO Live (ES)",
            language="es",
        )
        self.assertIs(localize_alert_item(item, enable_remote=False), item)

    def test_serebii_end_date_parsing(self) -> None:
        self.assertEqual(
            parse_serebii_end_date("November 18th - December 2nd 2026"),
            date(2026, 12, 2),
        )
        self.assertEqual(
            parse_serebii_end_date("November 24th - 30th 2026"),
            date(2026, 11, 30),
        )
        self.assertEqual(
            parse_serebii_end_date("September 5th 2021 11:00 - 17:00"),
            date(2021, 9, 5),
        )
        self.assertEqual(
            parse_serebii_end_date("December 28th 2019 11:00-19:00 local time"),
            date(2019, 12, 28),
        )
        self.assertIsNone(parse_serebii_end_date("Ongoing"))
        self.assertEqual(
            parse_serebii_start_date("November 18th - December 2nd 2026", date(2026, 12, 2)),
            date(2026, 11, 18),
        )
        self.assertEqual(
            parse_serebii_start_date("December 28th - January 4th 2027", date(2027, 1, 4)),
            date(2026, 12, 28),
        )

    def test_launch_day_reminder_fires_once_for_preannounced_event(self) -> None:
        state = default_state()
        ev = AlertItem(
            uid="serebii-tcgp:dual-fusion",
            game="tcgp",
            kind="event",
            title="Dual Fusion Limited Battles",
            summary="Desc",
            url="https://www.serebii.net/tcgpocket/events/dualfusionlimitedbattles.shtml",
            source_name="Serebii TCGP",
            starts_on="2026-11-18",
        )
        # Anunciado el 17 de nov -> inicia el 18 de nov
        mark_items_processed(state, [ev], now_utc=datetime(2026, 11, 17, 18, 5, tzinfo=timezone.utc))

        pending_day_of, _ = select_pending_alerts(
            state, [ev], now_utc=datetime(2026, 11, 18, 10, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(len(pending_day_of), 1)
        self.assertTrue(pending_day_of[0].is_launch_reminder)
        self.assertIn("¡YA DISPONIBLE HOY!", build_telegram_caption(pending_day_of[0]))

        mark_items_processed(
            state, pending_day_of, now_utc=datetime(2026, 11, 18, 10, 30, tzinfo=timezone.utc)
        )
        pending_again, _ = select_pending_alerts(
            state, [ev], now_utc=datetime(2026, 11, 18, 11, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(pending_again, [])

    def test_ptcgpocket_wp_posts_maps_events_and_news(self) -> None:
        posts = [
            {
                "date": "2026-11-18T01:00:00",
                "link": "https://ptcgpocket.gg/new-promo/",
                "title": {"rendered": "Promo Pack C &amp; Missions"},
                "categories": [7],
            },
            {
                "date": "2026-11-18T02:00:00",
                "link": "https://ptcgpocket.gg/new-expansion/",
                "title": {"rendered": "Dual Fusion Expansion"},
                "categories": [1, 8],
            },
        ]
        items = parse_ptcgpocket_wp_posts(posts)
        self.assertEqual([(it.kind, it.title) for it in items], [
            ("event", "Promo Pack C & Missions"),
            ("news", "Dual Fusion Expansion"),
        ])

    def test_bootstrap_limits_event_flood_and_keeps_codes(self) -> None:
        state = default_state()
        items = [
            AlertItem("c1", "pogo", "code", "Code 1", "S1", "https://a", "LeekDuck", promo_code="ABC123"),
            AlertItem("c2", "pogo", "code", "Code 2", "S2", "https://b", "LeekDuck", promo_code="XYZ789"),
            AlertItem("e1", "pogo", "event", "Event GO 1", "S3", "https://c", "ScrapedDuck"),
            AlertItem("e2", "pogo", "event", "Event GO 2", "S4", "https://d", "ScrapedDuck"),
            AlertItem("t1", "tcgp", "event", "Event TCG 1", "S5", "https://e", "Serebii TCGP"),
            AlertItem("t2", "tcgp", "event", "Event TCG 2", "S6", "https://f", "Serebii TCGP"),
        ]
        to_notify, to_seed = select_pending_alerts(state, items, max_alerts=8)
        self.assertEqual([x.uid for x in to_notify], ["c1", "c2", "e1", "t1"])
        self.assertEqual([x.uid for x in to_seed], ["e2", "t2"])

        mark_items_processed(state, to_notify + to_seed)
        next_notify, _ = select_pending_alerts(state, items)
        self.assertEqual(next_notify, [])

    def test_recurring_monthly_topic_not_blocked_after_ttl(self) -> None:
        state = default_state()
        first = AlertItem(
            "ptcgpocket-gg:community-week-7",
            "tcgp",
            "event",
            "Community Week Missions",
            "Missions",
            "https://ptcgpocket.gg/community-week-missions-7/",
            "PTCGPocket.gg",
        )
        t_nov = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        mark_items_processed(state, [first], now_utc=t_nov)

        # Mismo tema 2 días después desde otra fuente -> se deduplica (no spam)
        dup_other = AlertItem(
            "serebii-tcgp:community-week",
            "tcgp",
            "event",
            "Community Week Missions",
            "Missions",
            "https://www.serebii.net/tcgpocket/events/cw.shtml",
            "Serebii TCGP",
        )
        pending_dup, _ = select_pending_alerts(
            state, [dup_other], now_utc=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(pending_dup, [])

        # Mismo título genérico 30 días después con URL nueva -> sí debe notificar
        next_month = AlertItem(
            "ptcgpocket-gg:community-week-8",
            "tcgp",
            "event",
            "Community Week Missions",
            "Missions",
            "https://ptcgpocket.gg/community-week-missions-8/",
            "PTCGPocket.gg",
        )
        pending_next, _ = select_pending_alerts(
            state, [next_month], now_utc=datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual([x.uid for x in pending_next], ["ptcgpocket-gg:community-week-8"])

    def test_heartbeat_only_triggers_after_interval(self) -> None:
        state = default_state()
        t0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(update_source_health_and_heartbeat(state, [("Serebii GO", True)], had_state_change=False, now_utc=t0))

        t1 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(update_source_health_and_heartbeat(state, [("Serebii GO", True)], had_state_change=False, now_utc=t1))

        t2 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(update_source_health_and_heartbeat(state, [("Serebii GO", True)], had_state_change=False, now_utc=t2))

    def test_telegram_caption_escapes_html_and_respects_limit(self) -> None:
        long_summary = "Detalle <importante> & largo " * 60
        item = AlertItem(
            uid="pogo:long",
            game="pogo",
            kind="code",
            title="Recompensa <Special>",
            summary=long_summary,
            url="https://example.com",
            source_name="LeekDuck",
            promo_code="CODE&123",
            rewards="10x Poké Balls",
            expires_at="2027-01-15",
            language="es",
        )
        caption = build_telegram_caption(item)
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("<b>Pokémon GO</b>", caption)
        self.assertIn("<code>CODE&amp;123</code>", caption)
        self.assertIn("Recompensa &lt;Special&gt;", caption)

    def test_tcgp_caption_and_preview_selection(self) -> None:
        code_item = AlertItem("c1", "pogo", "code", "Zapatos", "Desc", "https://a", "LeekDuck", promo_code="ABC")
        tcgp_item = AlertItem("t1", "tcgp", "event", "Dual Fusion", "Desc", "https://b", "Serebii TCGP", language="es")
        caption = build_telegram_caption(tcgp_item)
        self.assertIn("<b>Pokémon TCG Pocket</b>", caption)
        self.assertNotIn("Tcg", caption)
        picked = _select_preview_sample([code_item, tcgp_item])
        self.assertEqual([x.uid for x in picked], ["c1", "t1"])

    def test_v2_enricher_serebii_and_deck_guide_matching(self) -> None:
        serebii_html = """
        <main>
          <p>This event is a special Limited Battle event using Dual Fusion cards only.</p>
          <table class="dextable">
            <tr><th>Effect</th><th>Type</th></tr>
            <tr><td>Catch Candy</td><td>2x</td></tr>
          </table>
          <table class="dextable">
            <tr><th>No.</th><th>Pic</th><th>Name</th><th>Type</th></tr>
            <tr><td>#0035</td><td></td><td>Clefairy</td><td></td></tr>
            <tr><td>#0744</td><td></td><td>Rockruff</td><td></td></tr>
          </table>
          <table class="dextable">
            <tr><th>Task</th><th>Reward</th></tr>
            <tr><td>Catch 3 Grass Pokémon</td><td>Bulbasaur</td></tr>
          </table>
        </main>
        """
        intro, bullets = parse_serebii_detail_html(serebii_html)
        self.assertIn("Dual Fusion", intro or "")
        joined = "\n".join(bullets)
        self.assertIn("Bonuses:", joined)
        self.assertIn("Clefairy, Rockruff", joined)
        self.assertIn("Catch 3 Grass Pokémon", joined)

        ptcg_html = """
        <div class="entry-content">
          <p>Players battle to earn special Emblems and Pack Hourglasses in versus mode.</p>
          <table>
            <tr><td>Mission Type</td><td>Reward</td></tr>
            <tr><th>Mission</th><th>Rewards</th></tr>
            <tr><td>Participate in 3 versus battles</td><td>6 Pack Hourglass</td></tr>
            <tr><td>Day 1</td><td>Log in</td><td>12 Pack Hourglass</td></tr>
          </table>
        </div>
        """
        p_intro, p_bullets = parse_ptcgpocket_detail_html(ptcg_html)
        self.assertIn("Emblems", p_intro or "")
        joined_p = "\n".join(p_bullets)
        self.assertIn("Participate in 3 versus battles", joined_p)
        self.assertIn("Day 1 (Log in) → 12 Pack Hourglass", joined_p)
        self.assertNotIn("Mission Type → Reward", joined_p)

        posts = [
            {"link": "https://ptcgpocket.gg/guide-1/", "title": {"rendered": "Event Guide"}},
            {"link": "https://ptcgpocket.gg/best-decks/", "title": {"rendered": "Best Decks: Dual Fusion Tier List"}},
        ]
        deck = select_best_deck_post(posts, exclude_url="https://ptcgpocket.gg/guide-1/")
        self.assertEqual(deck, ("https://ptcgpocket.gg/best-decks/", "Best Decks: Dual Fusion Tier List"))

        item = AlertItem(
            uid="t1",
            game="tcgp",
            kind="event",
            title="Dual Fusion",
            summary="Regla especial.",
            url="https://www.serebii.net/tcgpocket/events/dualfusionlimitedbattles.shtml",
            source_name="Serebii TCGP",
            guide_bullets=("Misiones: Jugar 3 partidas",),
            deck_guide_url="https://ptcgpocket.gg/best-decks/",
            deck_guide_title="Mejores mazos: Dual Fusion",
            language="es",
        )
        kb = build_inline_keyboard(item)
        btn_texts = [b["text"] for b in kb["inline_keyboard"][0]]
        self.assertIn("🃏 Mazos recomendados", btn_texts)

    def test_render_alert_card_creates_valid_png(self) -> None:
        code_item = AlertItem(
            uid="pogo:card",
            game="pogo",
            kind="code",
            title="Zapatillas adidas para tu Avatar",
            summary="Canjea este código promocional en la Tienda Web de Pokémon GO.",
            url="https://leekduck.com/promo-codes/",
            source_name="LeekDuck Promo",
            promo_code="POKEMONGOxCROCSxELECTRIFYING",
            rewards="Zapatillas adidas",
            expires_at="15 de enero de 2027",
            language="es",
        )
        event_item = AlertItem(
            uid="tcgp:card",
            game="tcgp",
            kind="event",
            title="Combates Limitados: Dual Fusion",
            summary="¡Evento activo en Pokémon TCG Pocket! Revisa misiones y sobres promo.",
            url="https://www.serebii.net/tcgpocket/events.shtml",
            source_name="Serebii TCGP",
            expires_at="18 de nov - 2 de dic de 2026",
            language="es",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for idx, sample in enumerate((code_item, event_item)):
                out_file = Path(tmpdir) / f"card_{idx}.png"
                render_alert_card(sample, out_file)
                self.assertTrue(out_file.exists())
                with Image.open(out_file) as img:
                    self.assertEqual(img.size, (1080, 600))

    @staticmethod
    def _sample_catalog() -> CardCatalog:
        expansions = [
            {"id": "pa", "name": "Promo-A"},
            {"id": "b2", "name": "Space-Time Smackdown"},
            {"id": "c1", "name": "Dual Fusion"},
        ]
        raw_cards = [
            # Dragon non-ex Stage 1 line in C1 + Dragon Mega ex line in C1
            {"id": "c1-050", "name": "Dratini", "set_code": "c1", "type": "Pokémon", "subtype": "Dragon", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 70, "retreat": 1, "attacks": {"1": {"cost": "W", "damage": "20"}}, "deckBuilderNr": 1001},
            {"id": "c1-051", "name": "Dragonair", "set_code": "c1", "type": "Pokémon", "subtype": "Dragon", "stage": "Stage 1", "evolves_from": "Dratini", "rarity": "◊◊", "ex": False, "mega": False, "health": 100, "retreat": 1, "attacks": {"1": {"cost": "WL", "damage": "70"}}, "deckBuilderNr": 1002},
            {"id": "c1-052", "name": "Mega Garchomp ex", "set_code": "c1", "type": "Pokémon", "subtype": "Dragon", "stage": "Basic", "rarity": "◊◊◊◊", "ex": True, "mega": True, "health": 210, "retreat": 2, "attacks": {"1": {"cost": "FF", "damage": "160"}}, "deckBuilderNr": 1003},
            # Psychics / Lightning / Water lines in C1 for variety
            {"id": "c1-020", "name": "Inkay", "set_code": "c1", "type": "Pokémon", "subtype": "Psychic", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "P", "damage": "20"}}, "deckBuilderNr": 1004},
            {"id": "c1-021", "name": "Malamar", "set_code": "c1", "type": "Pokémon", "subtype": "Psychic", "stage": "Stage 1", "evolves_from": "Inkay", "rarity": "◊◊", "ex": False, "mega": False, "health": 100, "retreat": 1, "attacks": {"1": {"cost": "PP", "damage": "90"}}, "deckBuilderNr": 1005},
            {"id": "c1-030", "name": "Mega Mewtwo Y ex", "set_code": "c1", "type": "Pokémon", "subtype": "Psychic", "stage": "Basic", "rarity": "◊◊◊◊", "ex": True, "mega": True, "health": 170, "retreat": 2, "attacks": {"1": {"cost": "PP", "damage": "150"}}, "deckBuilderNr": 1006},
            {"id": "c1-010", "name": "Pikachu", "set_code": "c1", "type": "Pokémon", "subtype": "Lightning", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "L", "damage": "30"}}, "deckBuilderNr": 1007},
            {"id": "c1-011", "name": "Raichu", "set_code": "c1", "type": "Pokémon", "subtype": "Lightning", "stage": "Stage 1", "evolves_from": "Pikachu", "rarity": "◊◊◊", "ex": False, "mega": False, "health": 110, "retreat": 1, "attacks": {"1": {"cost": "LL", "damage": "120"}}, "deckBuilderNr": 1008},
            {"id": "c1-040", "name": "Froakie", "set_code": "c1", "type": "Pokémon", "subtype": "Water", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "W", "damage": "20"}}, "deckBuilderNr": 1009},
            {"id": "c1-041", "name": "Greninja", "set_code": "c1", "type": "Pokémon", "subtype": "Water", "stage": "Stage 1", "evolves_from": "Froakie", "rarity": "◊◊◊", "ex": False, "mega": False, "health": 120, "retreat": 1, "attacks": {"1": {"cost": "WW", "damage": "100"}}, "deckBuilderNr": 1010},
            {"id": "c1-060", "name": "Eevee", "set_code": "c1", "type": "Pokémon", "subtype": "Colorless", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "C", "damage": "20"}}, "deckBuilderNr": 1011},
            # Trainers (Promo-A staples + B2 + C1)
            {"id": "pa-001", "name": "Potion", "set_code": "pa", "type": "Trainer", "subtype": "Item", "rarity": "Promo", "deckBuilderNr": 1_000_001},
            {"id": "pa-002", "name": "X Speed", "set_code": "pa", "type": "Trainer", "subtype": "Item", "rarity": "Promo", "deckBuilderNr": 1_000_002},
            {"id": "pa-005", "name": "Poké Ball", "set_code": "pa", "type": "Trainer", "subtype": "Item", "rarity": "Promo", "deckBuilderNr": 1_000_005},
            {"id": "pa-007", "name": "Professor's Research", "set_code": "pa", "type": "Trainer", "subtype": "Supporter", "rarity": "Promo", "deckBuilderNr": 1_000_007},
            {"id": "b2-090", "name": "Cyrus", "set_code": "b2", "type": "Trainer", "subtype": "Supporter", "rarity": "◊◊", "deckBuilderNr": 1_000_090},
            {"id": "c1-090", "name": "Serena's Choice", "set_code": "c1", "type": "Trainer", "subtype": "Supporter", "rarity": "◊◊", "deckBuilderNr": 1_000_101},
            {"id": "c1-091", "name": "Genome Converter Y", "set_code": "c1", "type": "Trainer", "subtype": "Item", "rarity": "◊◊", "card_text": "Move Psychic Energy to Mega Mewtwo Y ex.", "deckBuilderNr": 1_000_102},
            {"id": "c1-092", "name": "Eviolite", "set_code": "c1", "type": "Trainer", "subtype": "Pokemon Tool", "rarity": "◊◊", "deckBuilderNr": 1_000_103},
            {"id": "c1-093", "name": "Giant Cape", "set_code": "c1", "type": "Trainer", "subtype": "Pokemon Tool", "rarity": "◊◊", "deckBuilderNr": 1_000_104},
            {"id": "c1-094", "name": "Sabrina", "set_code": "c1", "type": "Trainer", "subtype": "Supporter", "rarity": "◊◊", "deckBuilderNr": 1_000_105},
            {"id": "c1-095", "name": "Rocky Helmet", "set_code": "c1", "type": "Trainer", "subtype": "Pokemon Tool", "rarity": "◊◊", "deckBuilderNr": 1_000_106},
            {"id": "c1-096", "name": "Super Potion", "set_code": "c1", "type": "Trainer", "subtype": "Item", "rarity": "◊", "deckBuilderNr": 1_000_107},
        ]
        return CardCatalog(raw_cards, expansions)

    def test_v3_qr_payload_encode_decode_roundtrip(self) -> None:
        import base64

        tr_ids = [70, 70, 50, 50]
        pk_ids = [10010, 10010, 10020, 10020]
        energies = [3, 4]
        payload = encode_deck_payload(tr_ids, pk_ids, energies)
        raw = base64.b64decode(payload)
        self.assertEqual(raw[0], 4)
        self.assertEqual(int.from_bytes(raw[1:4], "big"), 70 + TRAINER_ID_OFFSET)
        dec_tr, dec_pk, dec_en = decode_deck_payload(payload)
        self.assertEqual(dec_tr, tr_ids)
        self.assertEqual(dec_pk, pk_ids)
        self.assertEqual(dec_en, energies)

    def test_v3_extract_constraints_and_validate_deck(self) -> None:
        catalog = self._sample_catalog()
        limited_html = """
        <main>
          <p>Limited Battle event: You can only use cards from Dual Fusion in this event.</p>
          <table class="dextable">
            <tr><th>Card</th></tr>
            <tr><td><a>Deoxys</a></td><td><a>Mega Mewtwo Y ex</a></td></tr>
          </table>
        </main>
        """
        c_lim = extract_event_constraints(
            title="Dual Fusion Limited Battles",
            detail_html=limited_html,
            catalog=catalog,
        )
        self.assertTrue(c_lim.is_combat_event)
        self.assertEqual(c_lim.allowed_sets, ("c1",))
        self.assertIn("Mega Mewtwo Y ex", c_lim.allowed_promo_names)

        drop_html = """
        <main>
          <p>Mega Garchomp ex Drop Event Solo Battles.</p>
          <ul>
            <li>Win this battle without any Pokémon ex in your deck.</li>
            <li>Put 1 or more Mega Evolution Pokémon ex into play.</li>
            <li>Knock Out an opponent's Pokémon with damage from a Dragon-type Pokémon.</li>
            <li>Win this battle using a deck with 12 or more Dual Fusion cards.</li>
          </ul>
        </main>
        """
        c_drop = extract_event_constraints(
            title="Mega Garchomp ex Drop Event",
            detail_html=drop_html,
            catalog=catalog,
        )
        self.assertTrue(c_drop.is_combat_event)
        self.assertTrue(c_drop.no_ex)
        self.assertTrue(c_drop.require_mega_ex)
        self.assertIn("Dragon", c_drop.required_types)
        self.assertEqual(c_drop.min_set_cards, ("c1", 12))

        by_name = {c.name: c for c in catalog.cards}
        eighteen_trainers = [
            by_name[n]
            for n in (
                "Potion", "X Speed", "Poké Ball", "Professor's Research",
                "Cyrus", "Serena's Choice", "Sabrina", "Giant Cape", "Rocky Helmet",
            )
            for _ in range(2)
        ]

        # Baraja inválida: Stage 1 sin Básico (2 copias x 9 entrenadores = 18 + 2 Dragonair)
        bad_evo = [by_name["Dragonair"]] * 2 + eighteen_trainers
        ok_evo, msg_evo = validate_deck(bad_evo, [3], c_drop)
        self.assertFalse(ok_evo)
        self.assertIn("Básico", msg_evo)

        # Baraja con básico + 3 copias de Potion (>2) -> inválida
        bad_dup = [by_name["Dratini"]] * 2 + [by_name["Potion"]] * 3 + eighteen_trainers[:15]
        ok_dup, msg_dup = validate_deck(bad_dup, [3], c_drop)
        self.assertFalse(ok_dup)
        self.assertIn("2 copias", msg_dup)

        # Baraja con ex prohibido cuando require_no_ex=True -> inválida
        bad_ex = [by_name["Mega Garchomp ex"]] * 2 + eighteen_trainers
        ok_ex, msg_ex = validate_deck(bad_ex, [2], c_drop, require_no_ex=True)
        self.assertFalse(ok_ex)
        self.assertIn("ex", msg_ex)

        # Baraja con < 12 cartas C1 cuando min_set=("c1", 12) -> inválida
        not_enough_c1 = [by_name["Inkay"]] * 2 + eighteen_trainers
        ok_set, msg_set = validate_deck(not_enough_c1, [5], c_drop, min_set=("c1", 12))
        self.assertFalse(ok_set)
        self.assertIn("12", msg_set)

    def test_v3_build_three_decks_render_sheet_and_caption(self) -> None:
        catalog = self._sample_catalog()
        constraints = EventDeckConstraints(
            is_combat_event=True,
            no_ex=True,
            require_mega_ex=True,
            required_types=("Dragon",),
            min_set_cards=("c1", 12),
            target_expansion="c1",
        )
        decks = build_three_decks_for_event(
            event_title="Mega Garchomp ex Drop Event",
            event_summary="Evento de combate con restricciones.",
            constraints=constraints,
            catalog=catalog,
        )
        self.assertEqual(len(decks), 3)
        for deck in decks:
            self.assertEqual(len(deck.cards), 20)
            dec_tr, dec_pk, dec_en = decode_deck_payload(deck.qr_payload)
            self.assertEqual(len(dec_tr) + len(dec_pk), 20)
            self.assertTrue(1 <= len(dec_en) <= 3)

        # Opción 1 cumple Sin ex, Opción 2 incluye Mega ex, Opción 3 alcanza >=12 cartas C1
        self.assertFalse(any(c.is_ex or c.is_mega for c in decks[0].cards))
        self.assertTrue(any(c.is_mega for c in decks[1].cards))
        self.assertGreaterEqual(sum(1 for c in decks[2].cards if c.set_code == "c1"), 12)

        with tempfile.TemporaryDirectory() as tmpdir:
            card_path = Path(tmpdir) / "deck_single.png"
            render_single_deck_card(
                decks[0],
                card_path,
                context_label="Evento: Mega Garchomp ex Drop Event",
                badge_label="EVENTO TCG POCKET",
            )
            self.assertTrue(card_path.exists())
            with Image.open(card_path) as img:
                self.assertEqual(img.size, (1080, 720))

        dl_url = build_qr_download_url(decks[0].qr_payload)
        self.assertIn("download=1", dl_url)
        self.assertIn("api.qrserver.com", dl_url)
        strat = describe_deck_strategy(decks[0])
        self.assertIn("Atacante clave", strat)
        self.assertIn("Motor:", strat)

        caption = build_single_deck_caption(
            decks[0],
            context_title="Mega Garchomp ex Drop Event",
            header_tag="🃏 <b>Mazo recomendado</b>",
        )
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("Opción 1", caption)
        self.assertIn("Explicación", caption)
        kb = build_single_deck_keyboard(
            decks[0],
            info_label="🔎 Ver evento",
            info_url="https://www.serebii.net/tcgpocket/events/megagarchompexdropevent.shtml",
        )
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)
        self.assertEqual(kb["inline_keyboard"][0][0]["text"], "📥 Descargar QR")

    def test_weekly_meta_parsers_schedule_and_caption(self) -> None:
        cutoff = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
        tourn_html = """
        <table>
          <tr data-date="2026-11-22T05:00:00.000Z" data-format="1" data-players="160" data-name="Pocket Clash">
            <td><a href="/tournament/t1/standings">Pocket Clash</a></td>
          </tr>
          <tr data-date="2026-11-21T12:00:00.000Z" data-format="0" data-players="150" data-name="No-Ex Special">
            <td><a href="/tournament/t2/standings">No-Ex Special</a></td>
          </tr>
          <tr data-date="2026-11-20T12:00:00.000Z" data-format="1" data-players="12" data-name="Mini Lobby">
            <td><a href="/tournament/t3/standings">Mini Lobby</a></td>
          </tr>
          <tr data-date="2026-11-10T12:00:00.000Z" data-format="1" data-players="200" data-name="Old Tourney">
            <td><a href="/tournament/t4/standings">Old Tourney</a></td>
          </tr>
        </table>
        """
        tourns, reached = parse_completed_tournaments_html(tourn_html, cutoff_utc=cutoff, min_players=24)
        self.assertTrue(reached)
        self.assertEqual([t.tournament_id for t in tourns], ["t1"])

        standings_html = """
        <table>
          <tr data-placing="1">
            <td>1</td><td>Campeon</td><td></td><td>30</td><td>10 - 1 - 0</td>
            <td><a href="/tournament/t1/metagame/pikachu-ex"><span data-tooltip="Pikachu ex"></span></a></td>
            <td><a href="/tournament/t1/player/campeon/decklist">List</a></td>
          </tr>
        </table>
        """
        rows = parse_tournament_standings_rows(standings_html)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["placing"], rows[0]["wins"], rows[0]["name"]), (1, 10, "Pikachu ex"))

        catalog = self._sample_catalog()
        dual_dl_html = """
        <div class="decklist">
          <a href="https://pocket.limitlesstcg.com/cards/C1/40">2 Froakie</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/41">2 Greninja</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/7">2 Professor's Research</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/5">2 Poké Ball</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/1">2 Potion</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/2">2 X Speed</a>
          <a href="https://pocket.limitlesstcg.com/cards/B2/90">2 Cyrus</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/90">2 Serena's Choice</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/94">2 Sabrina</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/93">2 Giant Cape</a>
        </div>
        <div class="decklist">
          <a href="https://pocket.limitlesstcg.com/cards/C1/10">2 Pikachu</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/11">2 Raichu</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/7">2 Professor's Research</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/5">2 Poké Ball</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/1">2 Potion</a>
          <a href="https://pocket.limitlesstcg.com/cards/P-A/2">2 X Speed</a>
          <a href="https://pocket.limitlesstcg.com/cards/B2/90">2 Cyrus</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/90">2 Serena's Choice</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/94">2 Sabrina</a>
          <a href="https://pocket.limitlesstcg.com/cards/C1/93">2 Giant Cape</a>
        </div>
        """
        picked_cards = parse_limitless_decklist_cards(
            dual_dl_html,
            target_archetype_name="Pikachu Raichu",
            catalog=catalog,
        )
        self.assertEqual(len(picked_cards), 20)
        self.assertTrue(any(c.name == "Pikachu" for c in picked_cards))

        # Ventana de envío solo los lunes >=01:00 COT y máximo 1 vez por semana ISO
        st = default_state()
        wed_dt = datetime(2026, 11, 18, 13, 23, tzinfo=timezone.utc)
        mon_early = datetime(2026, 11, 23, 5, 23, tzinfo=timezone.utc)   # 00:23 COT
        mon_ok = datetime(2026, 11, 23, 6, 23, tzinfo=timezone.utc)      # 01:23 COT
        self.assertFalse(should_send_weekly_meta(st, now_utc=wed_dt)[0])
        self.assertFalse(should_send_weekly_meta(st, now_utc=mon_early)[0])
        fire_mon, wkey = should_send_weekly_meta(st, now_utc=mon_ok)
        self.assertTrue(fire_mon)
        st["last_weekly_meta_week"] = wkey
        self.assertFalse(should_send_weekly_meta(st, now_utc=mon_ok)[0])

        decks = build_three_decks_for_event(
            event_title="Ranked",
            event_summary="Top 3",
            constraints=EventDeckConstraints(is_combat_event=True),
            catalog=catalog,
        )
        report = WeeklyMetaReport(
            window_label="16/11 – 23/11/2026 (10 torneos Estándar)",
            analyzed_tournaments=10,
            analyzed_entries=420,
            archetypes=(
                WeeklyArchetypeSummary(
                    "pika",
                    "Pikachu ex",
                    40,
                    120,
                    80,
                    60.0,
                    "#1/160p en Clash",
                    decks[0],
                    "https://play.limitlesstcg.com/tournament/t1/player/campeon/decklist",
                ),
            ),
        )
        wcap = build_single_deck_caption(
            report.archetypes[0].deck,
            context_title="Top 3 Semanal Ranked",
            header_tag="🏆 <b>Mazo Ranked Semanal</b>",
            extra_note=report.archetypes[0].best_note,
        )
        wkb = build_single_deck_keyboard(
            report.archetypes[0].deck,
            info_label="🏆 Ver lista campeón",
            info_url=report.archetypes[0].best_url,
        )
        self.assertIn("Mazo Ranked Semanal", wcap)
        self.assertLessEqual(len(wcap), 1024)
        self.assertEqual(len(wkb["inline_keyboard"][0]), 2)

        # Ping único de prueba hoy 18/11 09:00-09:59 COT (nunca fuera ni repetido)
        st_ping = default_state()
        t_0838 = datetime(2026, 11, 18, 13, 38, tzinfo=timezone.utc)  # 08:38 COT
        t_0923 = datetime(2026, 11, 18, 14, 23, tzinfo=timezone.utc)  # 09:23 COT
        t_1023 = datetime(2026, 11, 18, 15, 23, tzinfo=timezone.utc)  # 10:23 COT
        t_next = datetime(2026, 11, 19, 14, 23, tzinfo=timezone.utc)  # 19/11 09:23 COT
        self.assertFalse(should_send_one_time_ping(st_ping, now_utc=t_0838)[0])
        ok_ping, ptag = should_send_one_time_ping(st_ping, now_utc=t_0923)
        self.assertTrue(ok_ping)
        self.assertEqual(ptag, "2026-11-18-09")
        st_ping["one_time_ping_sent"] = ptag
        self.assertFalse(should_send_one_time_ping(st_ping, now_utc=t_0923)[0])
        self.assertFalse(should_send_one_time_ping(default_state(), now_utc=t_1023)[0])
        self.assertFalse(should_send_one_time_ping(default_state(), now_utc=t_next)[0])

        # Regresión 10:23 AM: "(C1)" en título PTCGPocket.gg deduplica con Serebii y bloquea Cyrus B2
        ev_serebii = AlertItem("s1", "tcgp", "event", "Dual Fusion Limited Battles", "Desc", "https://s", "Serebii")
        ev_ptcg = AlertItem("p1", "tcgp", "event", "Dual Fusion (C1) Limited Battles", "Desc", "https://p", "PTCGPocket.gg")
        self.assertEqual(build_topic_key(ev_serebii), build_topic_key(ev_ptcg))

        ptcg_only_html = "<div class='entry-content'><p>Only cards from Dual Fusion can be used.</p></div>"
        c_ptcg = extract_event_constraints(title="Dual Fusion (C1) Limited Battles", detail_html=ptcg_only_html, catalog=catalog)
        self.assertEqual(c_ptcg.allowed_sets, ("c1",))

        # Deduplicación sin importar orden de palabras ni sufijo "Missions" / inicio ya notificado
        ev_m1 = AlertItem("m1", "tcgp", "event", "Second Anniversary Celebration Campaign Missions", "D", "https://m1", "Serebii")
        ev_m2 = AlertItem("m2", "tcgp", "event", "Second Anniversary Celebration Campaign", "D", "https://m2", "PTCGPocket.gg")
        self.assertEqual(build_topic_key(ev_m1), build_topic_key(ev_m2))

        st_dup = default_state()
        st_dup["initialized"] = True
        st_dup["seen_topics"]["tcgp:dual-fusion-limited-battles"] = "2026-11-18T12:00:00Z"
        st_dup["started_notified"]["tcgp:dual-fusion-limited-battles"] = "2026-11-18T12:00:00Z"
        ev_reordered = AlertItem(
            "p2",
            "tcgp",
            "event",
            "Limited Battles: Dual Fusion",
            "D",
            "https://p2",
            "PTCGPocket.gg",
            starts_on="2026-11-18",
        )
        pending_dup, _ = select_pending_alerts(
            st_dup, [ev_reordered], now_utc=datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(pending_dup, [])

    def test_v4_pro_enrichment_glossary_and_tournament_matching(self) -> None:
        ev_cday = {
            "extraData": {
                "communityday": {
                    "spawns": [{"name": "Pawniard"}],
                    "bonuses": [{"text": "1/4 Egg Hatch Distance"}, {"text": "2× Catch Candy"}],
                    "shinies": [{"name": "Pawniard"}, {"name": "Bisharp"}],
                },
                "raidbattles": {
                    "bosses": [{"name": "Darkrai"}],
                    "shinies": [{"name": "Darkrai"}],
                },
            }
        }
        bullets = parse_scrapedduck_extra(ev_cday)
        joined = "\n".join(bullets)
        self.assertIn("Pawniard", joined)
        self.assertIn("Shinies available: Pawniard, Bisharp, Darkrai", joined)

        es_title = translate_en_to_es("Best Decks & Meta Tier List", enable_remote=False)
        self.assertIn("Mejores Mazos", es_title)
        self.assertIn("Tier List del Meta", es_title)

        catalog = self._sample_catalog()
        constraints = EventDeckConstraints(is_combat_event=True, require_mega_ex=True, no_ex=False)
        noex_deck = build_three_decks_for_event(
            event_title="Drop Event No Ex",
            event_summary="",
            constraints=EventDeckConstraints(is_combat_event=True, no_ex=True),
            catalog=catalog,
        )[0]
        mega_deck = build_three_decks_for_event(
            event_title="Drop Event Mega",
            event_summary="",
            constraints=constraints,
            catalog=catalog,
        )[1]
        report = WeeklyMetaReport(
            window_label="11/11 – 18/11/2026",
            analyzed_tournaments=5,
            analyzed_entries=200,
            archetypes=(
                WeeklyArchetypeSummary("noex", "NoEx Fast", 20, 40, 20, 66.0, "#1/64p en Cup", noex_deck, "https://l/1"),
                WeeklyArchetypeSummary("mega", "Mega Boss", 30, 80, 30, 72.7, "#1/128p en Open", mega_deck, "https://l/2"),
            ),
        )
        picked_mega = pick_champion_deck_for_slot(
            report,
            constraints,
            slot_prefix="Opción 2",
            require_mega=True,
        )
        self.assertIsNotNone(picked_mega)
        self.assertIn("Mega Boss", picked_mega[0].title)
        self.assertEqual(picked_mega[1], "https://l/2")

        picked_no_ex = pick_champion_deck_for_slot(
            report,
            EventDeckConstraints(is_combat_event=True, no_ex=True),
            slot_prefix="Opción 1",
            require_no_ex=True,
        )
        self.assertIsNotNone(picked_no_ex)
        self.assertIn("NoEx Fast", picked_no_ex[0].title)

    def test_v4_open_drop_event_constraints_and_no_useless_rare_candy(self) -> None:
        expansions = [
            {"id": "a1", "name": "Genetic Apex"},
            {"id": "pa", "name": "Promo-A"},
            {"id": "c1", "name": "Dual Fusion"},
        ]
        raw_cards = [
            {"id": "c1-001", "name": "Eevee", "set_code": "c1", "type": "Pokémon", "subtype": "Colorless", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "C", "damage": "20"}}, "deckBuilderNr": 2001},
            {"id": "c1-010", "name": "Pikachu", "set_code": "c1", "type": "Pokémon", "subtype": "Lightning", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "L", "damage": "30"}}, "deckBuilderNr": 2002},
            {"id": "c1-011", "name": "Raichu", "set_code": "c1", "type": "Pokémon", "subtype": "Lightning", "stage": "Stage 1", "evolves_from": "Pikachu", "rarity": "◊◊◊", "ex": False, "mega": False, "health": 110, "retreat": 1, "attacks": {"1": {"cost": "LL", "damage": "120"}}, "deckBuilderNr": 2003},
            {"id": "c1-030", "name": "Inkay", "set_code": "c1", "type": "Pokémon", "subtype": "Psychic", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "P", "damage": "20"}}, "deckBuilderNr": 2004},
            {"id": "c1-031", "name": "Malamar", "set_code": "c1", "type": "Pokémon", "subtype": "Psychic", "stage": "Stage 1", "evolves_from": "Inkay", "rarity": "◊◊", "ex": False, "mega": False, "health": 100, "retreat": 1, "attacks": {"1": {"cost": "PP", "damage": "90"}}, "deckBuilderNr": 2005},
            {"id": "c1-040", "name": "Froakie", "set_code": "c1", "type": "Pokémon", "subtype": "Water", "stage": "Basic", "rarity": "◊", "ex": False, "mega": False, "health": 60, "retreat": 1, "attacks": {"1": {"cost": "W", "damage": "20"}}, "deckBuilderNr": 2006},
            {"id": "c1-041", "name": "Starmie", "set_code": "c1", "type": "Pokémon", "subtype": "Water", "stage": "Stage 1", "evolves_from": "Froakie", "rarity": "◊◊◊", "ex": False, "mega": False, "health": 100, "retreat": 1, "attacks": {"1": {"cost": "WW", "damage": "90"}}, "deckBuilderNr": 2007},
            {"id": "a1-200", "name": "Rare Candy", "set_code": "a1", "type": "Trainer", "subtype": "Item", "rarity": "◊◊", "card_text": "Choose 1 Basic Pokémon to evolve into Stage 2.", "deckBuilderNr": 1_000_200},
            {"id": "pa-005", "name": "Poké Ball", "set_code": "pa", "type": "Trainer", "subtype": "Item", "rarity": "Promo", "deckBuilderNr": 1_000_005},
            {"id": "pa-007", "name": "Professor's Research", "set_code": "pa", "type": "Trainer", "subtype": "Supporter", "rarity": "Promo", "deckBuilderNr": 1_000_007},
            {"id": "c1-090", "name": "Cyrus", "set_code": "c1", "type": "Trainer", "subtype": "Supporter", "rarity": "◊◊", "deckBuilderNr": 1_000_090},
            {"id": "c1-091", "name": "Sabrina", "set_code": "c1", "type": "Trainer", "subtype": "Supporter", "rarity": "◊◊", "deckBuilderNr": 1_000_091},
            {"id": "c1-092", "name": "Potion", "set_code": "pa", "type": "Trainer", "subtype": "Item", "rarity": "Promo", "deckBuilderNr": 1_000_092},
            {"id": "c1-093", "name": "X Speed", "set_code": "pa", "type": "Trainer", "subtype": "Item", "rarity": "Promo", "deckBuilderNr": 1_000_093},
            {"id": "c1-094", "name": "Giant Cape", "set_code": "c1", "type": "Trainer", "subtype": "Pokemon Tool", "rarity": "◊◊", "deckBuilderNr": 1_000_094},
            {"id": "c1-095", "name": "Rocky Helmet", "set_code": "c1", "type": "Trainer", "subtype": "Pokemon Tool", "rarity": "◊◊", "deckBuilderNr": 1_000_095},
        ]
        cat = CardCatalog(raw_cards, expansions)

        serebii_open_html = """
        <main>
          <h1>Mega Garchomp ex Drop Event</h1>
          <p>Win solo Step-Up Battles to earn Promo-B packs. Previous cards like Genetic Apex Mewtwo were featured. Archive: Limited Battle.</p>
        </main>
        """
        c_open = extract_event_constraints(
            title="Mega Garchomp ex Drop Event",
            detail_html=serebii_open_html,
            catalog=cat,
        )
        self.assertTrue(c_open.is_combat_event)
        self.assertIsNone(c_open.allowed_sets)
        self.assertEqual(c_open.target_expansion, "c1")

        decks = build_three_decks_for_event(
            event_title="Mega Garchomp ex Drop Event",
            event_summary="Evento abierto",
            constraints=c_open,
            catalog=cat,
        )
        self.assertEqual(len(decks), 3)
        for deck in decks:
            self.assertNotIn("Rare Candy", [c.name for c in deck.cards])

    def test_v5_pogo_intel_raid_hundo_gbl_and_render(self) -> None:
        # Fórmulas oficiales exactas de PC 100% IV (verificadas contra ScrapedDuck raids)
        self.assertEqual(calc_cp(285, 198, 172, 15, 15, 15, 20), 2136)
        self.assertEqual(calc_cp(285, 198, 172, 15, 15, 15, 25), 2671)
        self.assertEqual(calc_cp(152, 258, 260, 15, 15, 15, 20), 1633)
        self.assertEqual(calc_cp(152, 258, 260, 15, 15, 15, 25), 2041)

        weaks = dict(compute_weaknesses(("rock", "ground")))
        self.assertEqual(weaks["water"], 2.56)
        self.assertEqual(weaks["grass"], 2.56)
        self.assertEqual(weaks["fighting"], 1.6)

        raw_pokemon = [
            {"dex": 491, "speciesId": "darkrai", "speciesName": "Darkrai", "types": ["dark"], "baseStats": {"atk": 285, "def": 198, "hp": 172}, "fastMoves": ["SNARL"], "chargedMoves": ["DARK_PULSE"], "tags": ["mythical"], "defaultIVs": {"cp2500": [24, 10, 11, 13]}, "released": True, "family": {}},
            {"dex": 448, "speciesId": "lucario_mega", "speciesName": "Lucario (Mega)", "types": ["fighting", "steel"], "baseStats": {"atk": 310, "def": 175, "hp": 172}, "fastMoves": ["FORCE_PALM"], "chargedMoves": ["AURA_SPHERE"], "tags": ["mega"], "defaultIVs": {}, "released": True, "family": {}},
            {"dex": 448, "speciesId": "lucario", "speciesName": "Lucario", "types": ["fighting", "steel"], "baseStats": {"atk": 236, "def": 144, "hp": 172}, "fastMoves": ["COUNTER"], "chargedMoves": ["AURA_SPHERE"], "tags": [], "defaultIVs": {"cp1500": [19.5, 4, 15, 15]}, "released": True, "family": {}},
            {"dex": 639, "speciesId": "terrakion", "speciesName": "Terrakion", "types": ["rock", "fighting"], "baseStats": {"atk": 260, "def": 192, "hp": 209}, "fastMoves": ["DOUBLE_KICK"], "chargedMoves": ["SACRED_SWORD"], "tags": ["legendary"], "defaultIVs": {}, "released": True, "family": {}},
            {"dex": 68, "speciesId": "machamp_shadow", "speciesName": "Machamp (Shadow)", "types": ["fighting"], "baseStats": {"atk": 234, "def": 159, "hp": 207}, "fastMoves": ["COUNTER"], "chargedMoves": ["DYNAMIC_PUNCH"], "tags": ["shadow"], "defaultIVs": {"cp1500": [18, 4, 15, 15]}, "released": True, "family": {}},
            {"dex": 624, "speciesId": "pawniard", "speciesName": "Pawniard", "types": ["dark", "steel"], "baseStats": {"atk": 154, "def": 114, "hp": 128}, "fastMoves": ["FURY_CUTTER"], "chargedMoves": ["IRON_HEAD"], "tags": [], "defaultIVs": {"cp1500": [49, 4, 15, 15]}, "released": True, "family": {"evolutions": ["bisharp"]}},
            {"dex": 625, "speciesId": "bisharp", "speciesName": "Bisharp", "types": ["dark", "steel"], "baseStats": {"atk": 232, "def": 176, "hp": 163}, "fastMoves": ["SNARL"], "chargedMoves": ["DARK_PULSE"], "tags": [], "defaultIVs": {"cp2500": [38, 4, 15, 15]}, "released": True, "family": {"parent": "pawniard", "evolutions": ["kingambit"]}},
            {"dex": 983, "speciesId": "kingambit", "speciesName": "Kingambit", "types": ["dark", "steel"], "baseStats": {"atk": 238, "def": 203, "hp": 225}, "fastMoves": ["SNARL"], "chargedMoves": ["FOUL_PLAY", "IRON_HEAD"], "tags": [], "defaultIVs": {"cp2500": [25, 7, 15, 15]}, "released": True, "family": {"parent": "bisharp"}},
            {"dex": 809, "speciesId": "melmetal", "speciesName": "Melmetal", "types": ["steel"], "baseStats": {"atk": 226, "def": 190, "hp": 264}, "fastMoves": ["THUNDER_SHOCK"], "chargedMoves": ["DOUBLE_IRON_BASH", "SUPER_POWER"], "tags": ["mythical"], "defaultIVs": {"cp1500": [15, 4, 14, 15]}, "released": True, "family": {}},
        ]
        raw_moves = [
            {"moveId": "SNARL", "name": "Snarl", "type": "dark", "power": 5, "energy": 0, "energyGain": 13, "turns": 3},
            {"moveId": "DARK_PULSE", "name": "Dark Pulse", "type": "dark", "power": 80, "energy": 50, "energyGain": 0, "turns": 1},
            {"moveId": "FOUL_PLAY", "name": "Foul Play", "type": "dark", "power": 60, "energy": 40, "energyGain": 0, "turns": 1},
            {"moveId": "COUNTER", "name": "Counter", "type": "fighting", "power": 8, "energy": 0, "energyGain": 6, "turns": 2},
            {"moveId": "FORCE_PALM", "name": "Force Palm", "type": "fighting", "power": 13, "energy": 0, "energyGain": 10, "turns": 3},
            {"moveId": "DOUBLE_KICK", "name": "Double Kick", "type": "fighting", "power": 8, "energy": 0, "energyGain": 12, "turns": 3},
            {"moveId": "AURA_SPHERE", "name": "Aura Sphere", "type": "fighting", "power": 100, "energy": 55, "energyGain": 0, "turns": 1},
            {"moveId": "SACRED_SWORD", "name": "Sacred Sword", "type": "fighting", "power": 60, "energy": 35, "energyGain": 0, "turns": 1},
            {"moveId": "DYNAMIC_PUNCH", "name": "Dynamic Punch", "type": "fighting", "power": 90, "energy": 50, "energyGain": 0, "turns": 1},
            {"moveId": "FURY_CUTTER", "name": "Fury Cutter", "type": "bug", "power": 3, "energy": 0, "energyGain": 4, "turns": 1},
            {"moveId": "IRON_HEAD", "name": "Iron Head", "type": "steel", "power": 70, "energy": 50, "energyGain": 0, "turns": 1},
            {"moveId": "THUNDER_SHOCK", "name": "Thunder Shock", "type": "electric", "power": 3, "energy": 0, "energyGain": 9, "turns": 2},
            {"moveId": "DOUBLE_IRON_BASH", "name": "Double Iron Bash", "type": "steel", "power": 55, "energy": 35, "energyGain": 0, "turns": 1},
            {"moveId": "SUPER_POWER", "name": "Super Power", "type": "fighting", "power": 85, "energy": 40, "energyGain": 0, "turns": 1},
        ]
        rankings = {
            "great": [{"speciesId": "melmetal", "speciesName": "Melmetal", "score": 93.9, "moveset": ["THUNDER_SHOCK", "DOUBLE_IRON_BASH", "SUPER_POWER"], "counters": [{"opponent": "machamp_shadow"}]}],
            "ultra": [{"speciesId": "kingambit", "speciesName": "Kingambit", "score": 88.2, "moveset": ["SNARL", "FOUL_PLAY", "IRON_HEAD"], "counters": [{"opponent": "lucario"}]}],
            "master": [{"speciesId": "kingambit", "speciesName": "Kingambit", "score": 86.0, "moveset": ["SNARL", "FOUL_PLAY", "IRON_HEAD"], "counters": []}],
        }
        raids = [
            {"name": "Darkrai", "tier": "5-Star Raids", "canBeShiny": True, "types": [{"name": "dark"}], "boostedWeather": [{"name": "fog"}], "combatPower": {"normal": {"max": 2136}, "boosted": {"max": 2671}}},
        ]
        pogo_cat = PogoCatalog(raw_pokemon, raw_moves, rankings, raids)

        raid_card = build_raid_card("Darkrai", pogo_cat, context_title="Hora de Incursiones Darkrai")
        self.assertIsNotNone(raid_card)
        assert raid_card is not None
        self.assertIn("2136", raid_card.subtitle)
        self.assertIn("2671", raid_card.subtitle)
        # No duplica Lucario Mega + Lucario normal en la misma lista Top
        self.assertEqual(sum(1 for r in raid_card.right_rows if "Lucario" in r), 1)
        self.assertIn("Lucario", raid_card.search_string)

        cday_card = build_feature_pogo_card("Pawniard", pogo_cat, context_title="Día de la Comunidad Pawniard")
        self.assertIsNotNone(cday_card)
        assert cday_card is not None
        self.assertIn("Kingambit", cday_card.title)
        self.assertEqual(cday_card.search_string, "+Pawniard&4*,3*")

        weekly_cards = build_pogo_weekly_meta_report(pogo_cat)
        self.assertEqual(len(weekly_cards), 3)

        go_ev = AlertItem("scrapedduck:darkrai-hr", "pogo", "event", "Darkrai Raid Hour", "Desc", "https://ld", "ScrapedDuck")
        built_ev_cards = build_pogo_event_cards(go_ev, None, pogo_cat, context_title="Hora Legendaria Darkrai")
        self.assertEqual(len(built_ev_cards), 1)

        with tempfile.TemporaryDirectory() as tmp:
            png = render_pogo_tactical_card(raid_card, Path(tmp) / "pogo_raid.png")
            self.assertTrue(png.exists())
            with Image.open(png) as im:
                self.assertEqual(im.size, (1080, 720))

        cap = build_pogo_tactical_caption(raid_card)
        self.assertIn("<code>", cap)
        self.assertLessEqual(len(cap), 1024)
        kb = build_pogo_tactical_keyboard(raid_card, fallback_url="https://leekduck.com/boss/")
        self.assertEqual(kb["inline_keyboard"][0][0]["url"], "https://leekduck.com/boss/")

    def test_v6_weekly_codes_digest_and_unified_catalog(self) -> None:
        # 1. Ventana horaria de consolidado de códigos: Lunes >= 02:00 AM COT
        st = default_state()
        mon_0123_cot = datetime(2026, 11, 23, 6, 23, tzinfo=timezone.utc)  # Lunes 01:23 AM COT
        mon_0223_cot = datetime(2026, 11, 23, 7, 23, tzinfo=timezone.utc)  # Lunes 02:23 AM COT
        self.assertFalse(should_send_weekly_codes(st, now_utc=mon_0123_cot)[0])
        due, wkey = should_send_weekly_codes(st, now_utc=mon_0223_cot)
        self.assertTrue(due)
        st["last_weekly_codes_week"] = wkey
        self.assertFalse(should_send_weekly_codes(st, now_utc=mon_0223_cot)[0])

        # 2. Parser de códigos TCG Pocket descarta expirados y conserva vigentes
        tcgp_codes_html = """
        <table>
          <tr><th>Code</th><th>Release Date</th><th>Rewards</th><th>Expiry Date</th></tr>
          <tr><td>NEWGIFT2027</td><td>Nov 18, 2026</td><td>24 Pack Hourglass</td><td>Jan 31, 2027</td></tr>
          <tr><td>33V33S5MM3R</td><td>Jul 5, 2025</td><td>24 Pack Hourglass</td><td>Aug 31, 2025</td></tr>
        </table>
        """
        res_codes = parse_ptcgpocket_codes_html(tcgp_codes_html, now_utc=datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc))
        self.assertEqual([x.promo_code for x in res_codes.items], ["NEWGIFT2027"])

        # 3. Consolidado cruza fechas desde ScrapedDuck + LeekDuck + TCGP y renderiza tarjeta
        go_code = AlertItem(
            "leekduck-promo:ADIDASxPOKEMON",
            "pogo",
            "code",
            "adidas Shoes",
            "Canje",
            "https://ld",
            "LeekDuck Promo",
            promo_code="ADIDASxPOKEMON",
            redeem_url="https://store.pokemongo.com/offer-redemption?passcode=ADIDASxPOKEMON",
            rewards="adidas shoes",
            expires_at="January 15, 2027 at 11:59 PM",
            language="en",
        )
        events_cache = {
            "adidas-ev": {
                "eventID": "adidas-ev",
                "start": "2026-09-25T00:00:00Z",
                "end": "2027-01-15T23:59:00Z",
                "extraData": {"promocodes": ["ADIDASxPOKEMON"]},
            }
        }
        entries = build_active_code_entries(
            [go_code, res_codes.items[0]],
            events_cache,
            st,
            now_utc=datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc),
            enable_remote=False,
        )
        self.assertEqual(len(entries), 2)
        adidas = next(e for e in entries if e.code == "ADIDASxPOKEMON")
        self.assertEqual(adidas.start_date, "2026-09-25")
        self.assertEqual(adidas.end_date, "2027-01-15")
        self.assertIn("25 sep 2026", adidas.window_label)
        cap = build_weekly_codes_caption(entries, week_label=wkey)
        self.assertIn("<code>ADIDASxPOKEMON</code>", cap)
        self.assertIn("<code>NEWGIFT2027</code>", cap)
        kb = build_weekly_codes_keyboard(entries)
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            png_codes = render_weekly_codes_card(entries, tmp_dir / "codes.png", week_label=wkey)
            self.assertTrue(png_codes.exists())
            with Image.open(png_codes) as im:
                self.assertEqual(im.size, (1080, 720))

            # 4. Catálogo JSON unificado fusiona portales repetidos y es idempotente sin falsos cambios
            serebii_ev = AlertItem(
                "serebii-tcgp:df",
                "tcgp",
                "event",
                "Doble fusión Combates Limitados",
                "Desc",
                "https://serebii",
                "Serebii TCGP",
                language="es",
                guide_bullets=("Misiones: Abrir 1 sobre -> Sobre Promo C",),
                starts_on="2026-11-18",
                expires_at="2026-12-02",
            )
            ptcg_ev = AlertItem(
                "ptcgpocket-gg:df",
                "tcgp",
                "event",
                "Doble fusión (C1) Combates Limitados",
                "Desc 2",
                "https://ptcg",
                "PTCGPocket.gg",
                language="es",
                starts_on="2026-11-18",
            )
            hist_path = tmp_dir / "data" / "events_history.json"
            js_path = tmp_dir / "docs" / "events_data.js"
            records = [
                {"raw": AlertItem("leekduck-promo:ADIDASxPOKEMON", "pogo", "code", "Zapatos adidas", "Desc", "https://ld", "LeekDuck", promo_code="ADIDASxPOKEMON", rewards="Zapatos adidas", language="es"), "localized": AlertItem("leekduck-promo:ADIDASxPOKEMON", "pogo", "code", "Zapatos adidas", "Desc", "https://ld", "LeekDuck", promo_code="ADIDASxPOKEMON", rewards="Zapatos adidas", language="es"), "extra_ev": None, "events_cache": events_cache},
                {"raw": serebii_ev, "localized": serebii_ev, "extra_ev": None, "events_cache": events_cache, "tactical_summary": "3 mazos QR"},
                {"raw": ptcg_ev, "localized": ptcg_ev, "extra_ev": None, "events_cache": events_cache},
            ]
            doc1, ch1 = sync_events_catalog(history_path=hist_path, dashboard_js_path=js_path, records_input=records, now_utc=datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc))
            self.assertTrue(ch1)
            self.assertEqual(doc1["stats"]["total"], 2)  # Dual Fusion se fusionó en 1 evento único
            self.assertTrue(js_path.exists())
            self.assertIn("window.POKE_ALERTS_DB", js_path.read_text(encoding="utf-8"))
            loaded = load_events_history(hist_path)
            self.assertEqual(loaded["stats"]["codes_active"], 1)

            # Segunda ejecución idéntica NO marca cambio falso (evita commits vacíos cada hora)
            _, ch2 = sync_events_catalog(history_path=hist_path, dashboard_js_path=js_path, records_input=records, now_utc=datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc))
            self.assertFalse(ch2)


if __name__ == "__main__":
    unittest.main()
