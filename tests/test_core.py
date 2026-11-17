"""Pruebas unitarias deterministas (sin red) para poke-alerts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from PIL import Image

from src.card_renderer import render_alert_card
from src.enricher import (
    parse_ptcgpocket_detail_html,
    parse_serebii_detail_html,
    select_best_deck_post,
)
from src.main import _select_preview_sample
from src.models import AlertItem, build_topic_key
from src.scrapers import parse_leekduck_promos_html, parse_serebii_end_date
from src.state import (
    default_state,
    mark_items_processed,
    select_pending_alerts,
    update_source_health_and_heartbeat,
)
from src.telegram_client import build_inline_keyboard, build_telegram_caption
from src.translator import localize_alert_item, translate_en_to_es


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
            <tr><th>Mission</th><th>Rewards</th></tr>
            <tr><td>Participate in 3 versus battles</td><td>6 Pack Hourglass</td></tr>
          </table>
        </div>
        """
        p_intro, p_bullets = parse_ptcgpocket_detail_html(ptcg_html)
        self.assertIn("Emblems", p_intro or "")
        self.assertIn("Participate in 3 versus battles", "\n".join(p_bullets))

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


if __name__ == "__main__":
    unittest.main()
