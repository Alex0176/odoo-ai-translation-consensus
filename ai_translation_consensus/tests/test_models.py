# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import threading
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.base.models.ir_cron import MIN_TIME_PER_JOB
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from .. import providers
from ..engine import pipeline
from ..models import ai_translation_unit as unit_module
from ..models import ai_translation_watch as watch_module
from ..providers.base import ChatProvider
from ..providers.http import ProviderError, ProviderUnavailable


class ScriptedChat(ChatProvider):
    """Registers as kind 'scripted'; answers from ``cfg['extra']['script']``."""

    kind = "scripted"
    barrier = None
    label = "Scripted (tests)"
    needs_key = False
    defaults = {}

    def generate(self, kind, src, tgt, items, glossary=(), instructions="", page_context=""):
        if self.extra.get("fail"):
            raise ProviderError("down")
        if self.extra.get("outage"):
            raise ProviderUnavailable("usage limit reached", self.extra.get("wait"))
        if self.extra.get("barrier"):
            # only passes when that many groups reach the provider at the same moment
            ScriptedChat.barrier.wait(timeout=5)
        self._record(calls=1, input_tokens=100, output_tokens=40)
        table = self.extra.get("table", {})
        ScriptedChat.last_context = page_context
        ScriptedChat.last_glossary = list(glossary)
        return {str(i["id"]): {"text": table.get(i["text"], i["text"]), "note": ""}
                for i in items if i["text"] in table}

    def review(self, kind, src, tgt, items, glossary=(), instructions="", page_context="", round_no=1):
        self._record(calls=1, input_tokens=100, output_tokens=40)
        out = {}
        for item in items:
            scores, best = {}, ""
            for label, text, _w in item["cands"]:
                good = text in self.extra.get("good", [])
                scores[label] = (9.0, 9.0) if good else (5.0, 5.0)
                if good:
                    best = label
            out[str(item["id"])] = {"scores": scores, "best": best, "issues": {}, "suggestion": None}
        return out


@tagged("standard", "at_install")
class TestModels(TransactionCase):
    def setUp(self):
        super().setUp()
        unit_module._INFLIGHT.clear()
        unit_module._COOLDOWN.clear()
        self.addCleanup(unit_module._INFLIGHT.clear)
        self.addCleanup(unit_module._COOLDOWN.clear)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        providers.REGISTRY["scripted"] = ScriptedChat
        cls.addClassCleanup(providers.REGISTRY.pop, "scripted", None)
        cls.de = cls.env.ref("base.lang_de")
        cls.fr = cls.env.ref("base.lang_fr")
        (cls.de | cls.fr).active = True
        cls.env["ai.translation.provider"].search([]).write({"active": False})
        cls.partner_cat = cls.env["res.partner.category"].create({"name": "Machines"})
        cls.field = cls.env["ir.model.fields"]._get("res.partner.category", "name")
        cls.table = {"Machines": "Maschinen"}

    def _provider(self, name, **extra):
        return self.env["ai.translation.provider"].create({
            "name": name, "kind": "scripted", "extra_json": __import__("json").dumps(
                dict(table=self.table, good=list(self.table.values()), **extra))})

    def _watch(self, **vals):
        values = {
            "name": "test rule", "model_id": self.env["ir.model"]._get("res.partner.category").id,
            "domain": "[('id', '=', %d)]" % self.partner_cat.id, "field_ids": [(6, 0, self.field.ids)],
            "source_lang_id": self.env.ref("base.lang_en").id, "target_lang_ids": [(6, 0, self.de.ids)],
        }
        values.update(vals)
        return self.env["ai.translation.watch"].create(values)

    def test_translate_flow_with_human_approval(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch()
        self.assertEqual(watch._scan(), 1)
        unit = watch.unit_ids
        self.assertEqual((unit.state, unit.lang, unit.source_text), ("new", "de_DE", "Machines"))
        self.assertEqual(watch._scan(), 0)                      # no duplicates
        unit._process_group()
        self.assertEqual(unit.state, "consensus")
        self.assertEqual(unit.final_text, "Maschinen")
        self.assertEqual(len(unit.candidate_ids), 1)
        self.assertEqual(self.partner_cat.with_context(lang="de_DE").name, "Machines")   # not applied yet
        unit.action_approve()
        self.assertEqual(unit.state, "applied")
        self.assertEqual(self.partner_cat.with_context(lang="de_DE").name, "Maschinen")
        self.assertEqual(watch._scan(), 0)                      # translated, nothing missing

    def test_auto_apply(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch(auto_apply=True)
        watch._scan()
        watch.unit_ids._process_group()
        self.assertEqual(watch.unit_ids.state, "applied")

    def test_provider_failure_leaves_a_human_decision(self):
        self._provider("one", fail=True)
        watch = self._watch()
        watch._scan()
        watch.unit_ids._process_group()
        self.assertEqual(watch.unit_ids.state, "failed")
        self.assertIn("down", watch.unit_ids.error)

    def _outage_setup(self, kind="translate", **flags):
        self.table = {"Machines": "Maschinen"}
        prov = self._provider("one", **flags)
        watch = self._watch(kind=kind) if kind != "translate" else self._watch()
        watch._scan()
        return prov, watch

    def test_an_outage_keeps_the_unit_queued_and_pauses_the_provider(self):
        prov, watch = self._outage_setup(outage=True)
        unit = watch.unit_ids
        self.assertEqual(unit._process_group(), 0)
        self.assertEqual((unit.state, unit.attempts), ("new", 0))
        self.assertIn("usage limit reached", unit.error)
        self.assertTrue(prov.is_paused)
        self.assertEqual(prov.pause_level, 1)
        self.assertIn("usage limit", prov.pause_reason)
        self.assertEqual(watch._stats()["counts"]["failed"], 0)
        self.assertEqual(watch._stats()["queued"], 1)
        self.assertEqual([p["name"] for p in watch._stats()["paused"]], ["one"])
        self.assertEqual(watch._pick_providers(), ([], []))               # left alone during the pause
        self.assertTrue(watch._pick_providers(respect_pause=False)[0])
        self.assertIn((self.env.cr.dbname, unit.id), unit_module._COOLDOWN)

    def test_an_outage_of_one_provider_does_not_stop_the_others(self):
        broken, watch = self._outage_setup(outage=True)
        self._provider("two")
        self._provider("three")
        watch.unit_ids._process_group()
        self.assertEqual(watch.unit_ids.state, "consensus")
        self.assertTrue(broken.is_paused)
        self.assertEqual(len(watch._pick_providers()[0]), 2)

    def test_the_pause_grows_and_a_working_answer_ends_it(self):
        prov, watch = self._outage_setup(outage=True)
        for level, seconds in enumerate((300, 600, 1200), start=1):
            prov._note_outcome({"one": pipeline.Outage("limit")}, {"one": None})
            self.assertEqual(prov.pause_level, level)
            self.assertAlmostEqual((prov.paused_until - fields.Datetime.now()).total_seconds(), seconds, delta=5)
            prov._note_outcome({"one": pipeline.Outage("limit")}, {"one": None})     # still paused: no new level
            self.assertEqual(prov.pause_level, level)
            prov.paused_until = fields.Datetime.now() - timedelta(seconds=1)         # the pause ran out
        prov._note_outcome({}, {})
        self.assertEqual((prov.pause_level, prov.paused_until, prov.error_streak), (0, False, 0))

    def test_a_provider_that_names_the_wait_is_paused_that_long_but_not_longer_than_a_day(self):
        prov, _watch = self._outage_setup(outage=True)
        prov._note_outcome({"one": pipeline.Outage("quota")}, {"one": 36000})
        self.assertAlmostEqual((prov.paused_until - fields.Datetime.now()).total_seconds(), 36000, delta=5)
        prov.action_resume()
        prov._note_outcome({"one": pipeline.Outage("quota")}, {"one": 10 ** 7})
        self.assertAlmostEqual((prov.paused_until - fields.Datetime.now()).total_seconds(), 86400, delta=5)

    def test_a_used_up_plan_pauses_the_provider_until_the_reset(self):
        prov, _watch = self._outage_setup(outage=True)
        soon = fields.Datetime.to_string(fields.Datetime.now() + timedelta(days=3))
        windows = [{"name": "Weekly", "window": "", "used": 100.0, "resets_at": soon, "gates": True}]
        prov._pause_for_usage(windows, "")
        self.assertTrue(prov.is_paused)
        self.assertEqual((fields.Datetime.to_string(prov.paused_until), prov.pause_level), (soon, 0))
        self.assertTrue(prov.pause_reason.startswith("Plan usage used up"))
        prov._pause_for_usage([dict(windows[0], used=0.0)], "")       # reset credit redeemed
        self.assertEqual((prov.is_paused, prov.pause_reason), (False, False))
        prov._note_outcome({"one": pipeline.Outage("limit")}, {"one": None})   # an outage pause is left alone
        prov._pause_for_usage([dict(windows[0], used=0.0)], "")
        self.assertTrue(prov.is_paused)

    def test_other_errors_pause_the_provider_only_when_they_repeat(self):
        prov, _watch = self._outage_setup(fail=True)
        for streak in (1, 2):
            prov._note_outcome({"one": "down"}, {})
            self.assertEqual((prov.error_streak, prov.is_paused), (streak, False))
        prov._note_outcome({}, {})                                                    # one success: start over
        self.assertEqual(prov.error_streak, 0)
        for _i in range(3):
            prov._note_outcome({"one": "down"}, {})
        self.assertTrue(prov.is_paused)

    def test_a_working_test_ends_the_pause(self):
        prov, _watch = self._outage_setup()
        prov._note_outcome({"one": pipeline.Outage("limit")}, {"one": None})
        self.assertTrue(prov.is_paused)
        with patch.object(ScriptedChat, "ping", lambda self: "Guten Tag", create=True):
            prov.action_test()
        self.assertFalse(prov.is_paused)

    def test_an_outage_never_closes_a_proofread_unit_as_unchanged(self):
        prov, watch = self._outage_setup(kind="proofread", outage=True)
        unit = watch.unit_ids
        unit._process_group()
        self.assertEqual(unit.state, "new")
        self.assertFalse(unit.candidate_ids)

    def test_a_failing_provider_without_outage_still_fails_the_unit(self):
        prov, watch = self._outage_setup(fail=True)
        watch.unit_ids._process_group()
        self.assertEqual(watch.unit_ids.state, "failed")
        self.assertFalse(prov.is_paused)

    def test_failures_of_an_outage_can_be_queued_again_but_no_others(self):
        _prov, watch = self._outage_setup(fail=True)
        Unit = self.env["ai.translation.unit"]

        def failed(error, with_candidate=False):
            unit = Unit.create({
                "watch_id": watch.id, "model_name": "res.partner.category", "res_id": self.partner_cat.id,
                "field_name": "name", "lang": "fr_FR", "source_lang": "en_US",
                "source_text": error, "source_hash": pipeline.group_key(error), "kind": "translate",
                "state": "failed", "error": error})
            if with_candidate:
                self.env["ai.translation.candidate"].create({
                    "unit_id": unit.id, "key": "k", "text": "x", "authors": "a", "origin": "translation"})
            return unit

        outage = failed("Claude Code CLI (signed-in): exit code 1: ; Codex: exit code 1: banner")
        quota = failed("MyMemory (keyless): HTTP 429: MYMEMORY WARNING: YOU USED ALL")
        bug = failed("MyMemory (keyless): AttributeError: 'NoneType' object has no attribute 'upper'")
        content = failed("no valid candidate")
        rated = failed("Codex: exit code 1: x", with_candidate=True)
        self.assertEqual(watch._stats()["retryable"], 3)
        self.assertEqual(watch.dashboard_retry_failed(), 3)
        self.assertEqual((outage | quota | bug).mapped("state"), ["new"] * 3)
        self.assertEqual(content.state, "failed")
        self.assertEqual(rated.state, "failed")
        self.assertFalse(outage.error)
        self.assertEqual(watch.dashboard_retry_failed(), 0)
        self.assertEqual(content.action_retry_outage_failures()["params"]["type"], "warning")
        self.assertEqual(content.state, "failed")

    def test_no_provider_keeps_units_queued(self):
        watch = self._watch()
        watch._scan()
        self.assertEqual(watch.unit_ids._process_group(), 0)
        self.assertEqual(watch.unit_ids.state, "new")

    def test_stale_source_is_never_written(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch()
        watch._scan()
        unit = watch.unit_ids
        unit._process_group()
        self.partner_cat.name = "Machinery"
        self.assertFalse(unit._apply())
        self.assertEqual(unit.state, "stale")
        self.assertEqual(self.partner_cat.with_context(lang="de_DE").name, "Machinery")

    def test_manual_text_is_checked(self):
        self._provider("one")
        self._provider("two")
        self.partner_cat.name = "<b>Machines</b>"
        watch = self._watch()
        self.table = {"<b>Machines</b>": "<b>Maschinen</b>"}
        watch._scan()
        unit = watch.unit_ids
        unit.write({"state": "review", "manual_text": "Maschinen"})
        with self.assertRaises(UserError):
            unit.action_apply_manual()

    def test_confidential_rule_only_uses_local_providers(self):
        remote = self._provider("remote")
        local = self._provider("local")
        local.is_local = True
        watch = self._watch(confidential=True)
        translators, _reviewers = watch._pick_providers()
        self.assertEqual([t.name for t in translators], ["local"])
        self.assertTrue(remote)

    def test_providers_without_key_are_skipped(self):
        self.env["ai.translation.provider"].create({"name": "anthropic-no-key", "kind": "anthropic"})
        watch = self._watch()
        self.assertEqual(watch._pick_providers(), ([], []))

    def test_glossary_reaches_the_providers(self):
        self._provider("one")
        self.env["ai.translation.glossary"].create({"term": "Machines", "translation": "Anlagen"})
        watch = self._watch()
        watch._scan()
        watch.unit_ids._process_group()
        self.assertEqual(ScriptedChat.last_glossary, ['"Machines" -> "Anlagen"'])

    def test_long_texts_and_the_audit_sample_wait_for_a_human(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch(auto_apply=True, auto_apply_max_chars=5)
        watch._scan()
        unit = watch.unit_ids
        unit._process_group()
        self.assertEqual(unit.state, "consensus")
        self.assertIn("5", unit.reason)                   # the reason is written in the user's language
        unit.action_approve()
        self.assertEqual(unit.state, "applied")
        # every text is in the audit sample at 100 %, none at 0 %
        for percent, state in ((100.0, "consensus"), (0.0, "applied")):
            other = self.env["res.partner.category"].create({"name": "Machines"})
            watch2 = self._watch(name="audit %s" % percent, auto_apply=True, audit_percent=percent,
                                 domain="[('id', '=', %d)]" % other.id)
            watch2._scan()
            watch2.unit_ids._process_group()
            self.assertEqual(watch2.unit_ids.state, state)
            if percent:
                self.assertNotIn("agree", watch2.unit_ids.reason)          # replaced by the hold-back reason

    def test_an_earlier_translation_of_the_same_text_is_offered_as_candidate(self):
        self._provider("one")
        self._provider("two")
        first = self._watch()
        first._scan()
        earlier = first.unit_ids
        earlier._process_group()
        earlier.action_approve()
        earlier.final_text = "Maschinenpark"
        other = self.env["res.partner.category"].create({"name": "Machines"})
        second = self._watch(name="second", domain="[('id', '=', %d)]" % other.id)
        second._scan()
        unit = second.unit_ids
        self.assertEqual(unit._memory(), {str(unit.id): "Maschinenpark"})
        unit._process_group()
        by_origin = {c.origin: c for c in unit.candidate_ids}
        self.assertEqual(by_origin["memory"].text, "Maschinenpark")
        self.assertEqual(unit.final_text, "Maschinen")                   # the reviewers preferred the new text
        # nothing is offered for proofreading, and nothing from another language
        self.assertEqual(self.env["ai.translation.unit"]._memory(), {})

    def test_the_arbiter_is_only_used_when_it_can_be_trusted(self):
        arbiter = self._provider("arbiter")
        watch = self._watch(arbiter_id=arbiter.id)
        self.assertEqual(watch._pick_arbiter().name, "arbiter")
        arbiter.active = False
        self.assertIsNone(watch._pick_arbiter())
        arbiter.active = True
        watch.confidential = True
        self.assertIsNone(watch._pick_arbiter())              # not on our own infrastructure
        self.assertIsNone(self._watch(name="no arbiter")._pick_arbiter())

    def test_the_cross_checks_are_on_or_off_by_default_as_intended(self):
        watch = self._watch()
        self.assertTrue(watch.strict_checks)
        self.assertFalse(watch.back_check)
        self.assertFalse(watch.arbiter_id)
        self.assertEqual((watch.auto_apply_max_chars, watch.audit_percent), (0, 0.0))

    def test_glossary_entries_keep_the_difference_between_keep_and_translate(self):
        glossary = self.env["ai.translation.glossary"]
        glossary.create({"term": "Biricon"})
        glossary.create({"term": "support", "translation": "Support"})
        self.assertEqual(sorted(glossary._entries_for("de_DE", ["Biricon support", "x"])),
                         [("Biricon", ""), ("support", "Support")])
        self.assertEqual(glossary._entries_for("de_DE", ["nothing here"]), [])

    def test_the_review_keeps_what_the_candidate_was_checked_with(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch(back_check=True)
        watch._scan()
        unit = watch.unit_ids
        unit._process_group()
        winner = unit.candidate_ids.filtered("is_winner")
        self.assertEqual(winner.text, "Maschinen")
        self.assertFalse(winner.back_translation)             # a short text is not translated back

    def test_requeue(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch()
        watch._scan()
        unit = watch.unit_ids
        unit._process_group()
        unit.action_requeue()
        self.assertEqual((unit.state, len(unit.candidate_ids)), ("new", 0))

    def test_term_field_translates_each_block_and_keeps_markup(self):
        self.table = {"Hello": "Hallo", "World": "Welt"}
        self._provider("one")
        self._provider("two")
        tmpl = self.env["ir.actions.act_window"].create({
            "name": "AI test", "res_model": "res.partner", "help": "<p>Hello</p><p>World</p>"})
        watch = self._watch(
            model_id=self.env["ir.model"]._get("ir.actions.act_window").id,
            domain="[('id', '=', %d)]" % tmpl.id,
            field_ids=[(6, 0, self.env["ir.model.fields"]._get("ir.actions.act_window", "help").ids)],
            auto_apply=True)
        self.assertEqual(watch._scan(), 2)
        watch.unit_ids._process_group()
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"applied"})
        body = tmpl.with_context(lang="de_DE").help
        self.assertIn("Hallo", body)
        self.assertIn("Welt", body)
        self.assertIn("<p>", body)
        self.assertIn("Hello", tmpl.with_context(lang="en_US").help)
        self.assertEqual(watch._scan(), 0)

    def test_proofread_replaces_the_source_text(self):
        self.table = {"Machines": "Machines."}
        self._provider("one")
        self._provider("two")
        watch = self._watch(kind="proofread", auto_apply=True)
        self.assertEqual(watch._scan(), 1)
        watch.unit_ids._process_group()
        self.assertEqual(watch.unit_ids.state, "applied")
        self.assertEqual(self.partner_cat.with_context(lang="en_US").name, "Machines.")

    def test_providers_are_told_the_real_text_language(self):
        seen = []
        original = ScriptedChat.generate

        def spy(self_, kind, src, tgt, *args, **kw):
            seen.append((src, tgt))
            return original(self_, kind, src, tgt, *args, **kw)

        self._provider("one")
        watch = self._watch(text_lang_id=self.de.id)
        watch._scan()
        with patch.object(ScriptedChat, "generate", spy):
            watch.unit_ids._process_group()
        self.assertEqual(seen, [("de_DE", "de_DE")])

    def _wizard(self, **vals):
        values = {
            "model_id": self.env["ir.model"]._get("res.partner.category").id,
            "domain": "[('id', '=', %d)]" % self.partner_cat.id, "field_ids": [(6, 0, self.field.ids)],
            "source_lang_id": self.env.ref("base.lang_en").id, "target_lang_ids": [(6, 0, self.de.ids)],
        }
        values.update(vals)
        return self.env["ai.translation.wizard"].create(values)

    def test_wizard_steps_and_validation(self):
        wiz = self.env["ai.translation.wizard"].create({})
        self.assertEqual(wiz.step, "intro")
        wiz.action_next()
        self.assertEqual(wiz.step, "what")
        with self.assertRaises(UserError):
            wiz.action_next()  # no model chosen
        wiz = self._wizard()
        wiz.step = "what"
        wiz.action_next()
        wiz.action_next()
        self.assertEqual(wiz.step, "done")
        wiz.action_back()
        self.assertEqual(wiz.step, "who")
        wiz = self._wizard(target_lang_ids=[(5,)])
        wiz.step = "what"
        with self.assertRaises(UserError):
            wiz.action_next()  # translating needs a target language

    def test_wizard_creates_a_safe_rule_and_scans(self):
        self._provider("one")
        self._provider("two")
        wiz = self._wizard(instructions="Formal")
        wiz.step = "done"
        self.assertIn("one", wiz.summary)
        self.assertIn("two", wiz.summary)
        action = wiz.action_create()
        watch = self.env["ai.translation.watch"].search([("instructions", "=", "Formal")])
        self.assertEqual(len(watch), 1)
        self.assertFalse(watch.auto_apply)
        self.assertFalse(watch.auto_scan)
        self.assertEqual(watch.target_lang_ids, self.de)
        self.assertEqual(action["res_model"], "ai.translation.unit")
        self.assertEqual(len(watch.unit_ids), 1)

    def test_wizard_blocks_creation_without_provider(self):
        wiz = self._wizard()
        wiz.step = "done"
        self.assertIn("No usable provider", wiz.with_context(lang="en_US").summary)
        with self.assertRaises(UserError):
            wiz.action_create()
        self.assertFalse(self.env["ai.translation.watch"].search([("model_id", "=", wiz.model_id.id)]))

    def test_wizard_language_shortcuts(self):
        wiz = self._wizard()
        wiz.action_all_languages()
        self.assertNotIn(self.env.ref("base.lang_en"), wiz.target_lang_ids)
        self.assertIn(self.fr, wiz.target_lang_ids)

    def test_rule_validation(self):
        with self.assertRaises(Exception):
            self._watch(domain="not a domain")
        with self.assertRaises(Exception):
            self._watch(threshold=0)

    def test_provider_key_from_environment(self):
        import os
        prov = self.env["ai.translation.provider"].create({
            "name": "env", "kind": "openai", "api_key_env": "AI_TRANSLATION_TEST_KEY"})
        os.environ["AI_TRANSLATION_TEST_KEY"] = "from-env"
        self.addCleanup(os.environ.pop, "AI_TRANSLATION_TEST_KEY", None)
        self.assertEqual(prov._config()["api_key"], "from-env")
        self.assertFalse(prov._config()["allow_command"])

    # ------------------------------------------------------------ usage, cost, dashboard, completion
    def test_usage_is_logged_and_priced(self):
        one = self._provider("one")
        self._provider("two")
        one.write({"price_input": 3.0, "price_output": 15.0})
        watch = self._watch()
        watch._scan()
        watch.unit_ids._process_group()
        usage = self.env["ai.translation.usage"].search([("watch_id", "=", watch.id)])
        self.assertEqual(set(usage.mapped("provider_name")), {"one", "two"})
        row = usage.filtered(lambda u: u.provider_name == "one")
        self.assertEqual((row.calls, row.input_tokens, row.output_tokens), (1, 100, 40))
        self.assertAlmostEqual(row.cost, (100 * 3.0 + 40 * 15.0) / 1e6, places=6)
        self.assertTrue(row.priced)
        self.assertTrue(watch.unit_ids.processed_at)
        self.assertGreater(watch.unit_ids.process_seconds, 0)

    def test_dashboard_numbers_and_forecast(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch()
        watch._scan()
        data = self.env["ai.translation.watch"].get_dashboard()
        rule = next(r for r in data["rules"] if r["id"] == watch.id)
        self.assertEqual((rule["total"], rule["counts"]["new"], rule["closed"]), (1, 1, 0))
        self.assertFalse(rule["forecast"]["ready"])
        watch.unit_ids._process_group()
        rule = next(r for r in self.env["ai.translation.watch"].get_dashboard()["rules"] if r["id"] == watch.id)
        self.assertEqual(rule["counts"]["consensus"], 1)
        self.assertEqual((rule["tokens_in"], rule["tokens_out"]), (200, 80))
        self.assertEqual(rule["languages"][0]["code"], "de_DE")
        self.assertTrue(rule["cost_known"])           # free providers cost nothing
        self.assertTrue(rule["forecast"]["ready"])

    def test_forecast_uses_the_measured_cost_per_text(self):
        one = self._provider("one")
        self._provider("two")
        one.write({"price_input": 10.0, "price_output": 10.0})
        self.env["res.partner.category"].create({"name": "Tools"})
        watch = self._watch(domain="[('id', 'in', %s)]" % (
            self.partner_cat.ids + self.env["res.partner.category"].search([("name", "=", "Tools")]).ids))
        watch._scan()
        self.assertEqual(len(watch.unit_ids), 2)
        first = watch.unit_ids[0]
        first._process_group()
        rule = watch._stats()
        self.assertEqual(rule["queued"], 1)
        per_text = rule["cost"]
        self.assertAlmostEqual(rule["forecast"]["cost"], per_text, places=6)

    def test_unpriced_paid_provider_is_reported(self):
        self._provider("paid")
        usage = self.env["ai.translation.usage"]
        watch = self._watch()
        usage.create({"watch_id": watch.id, "provider_name": "paid", "units": 1, "calls": 1,
                      "input_tokens": 10, "priced": False})
        self.assertEqual(watch._stats()["unpriced"], ["paid"])

    def test_completion_message_is_posted_once_and_silently(self):
        self._provider("one")
        self._provider("two")
        channel = self.env.ref("ai_translation_consensus.channel_ai_translation")
        mails = self.env["mail.mail"].search_count([])
        emails = self.env["mail.notification"].search_count([("notification_type", "=", "email")])
        before = len(channel.message_ids)
        watch = self._watch().with_context(lang="en_US")
        watch._scan()
        unit = watch.unit_ids
        unit._process_group()
        self.assertFalse(watch.completion_notified)           # still waits for approval
        unit.action_approve()
        self.assertTrue(watch.completion_notified)
        new = channel.message_ids[: len(channel.message_ids) - before]
        self.assertEqual(len(new), 1)
        self.assertIn("All languages translated", new.body)
        self.assertEqual(self.env["mail.mail"].search_count([]), mails)
        self.assertEqual(self.env["mail.notification"].search_count([("notification_type", "=", "email")]),
                         emails)
        self.partner_cat.name = "Machinery"                   # a changed text is new work
        watch._scan()
        self.assertFalse(watch.completion_notified)

    def test_changed_records_start_the_rule_again(self):
        self._provider("one")
        self._provider("two")
        watch = self._watch()
        self.assertEqual(watch._scan(), 1)
        self.assertTrue(watch.scan_since)
        self.assertEqual(watch.scan_cursor, 0)
        # untouched for an hour: the cron scan does not even look at it
        self.env.cr.execute("UPDATE res_partner_category SET write_date = now() - interval '1 hour' "
                            "WHERE id = %s", [self.partner_cat.id])
        self.partner_cat.invalidate_recordset()
        watch.scan_since = self.env.cr.now()
        with patch.object(type(self.env["ai.translation.unit"]), "_scan_field",
                          return_value=0) as scan:
            self.assertEqual(watch._scan(full=False), 0)
            scan.assert_not_called()
        self.partner_cat.name = "Machinery"
        self.assertEqual(watch._scan(full=False), 1)
        self.assertEqual(len(watch.unit_ids), 2)

    def _cron_setup(self, count=3, **watch_vals):
        self.env["ai.translation.watch"].search([]).write({"auto_scan": False})
        names = ["Alpha", "Beta", "Gamma", "Delta"][:count]
        cats = self.env["res.partner.category"].create([{"name": n} for n in names])
        self.table = {n: n + "-de" for n in names}
        self._provider("one")
        self._provider("two")
        watch_vals.setdefault("parallel_groups", 1)
        watch = self._watch(domain="[('id', 'in', %s)]" % cats.ids, auto_scan=True, batch_size=1,
                            **watch_vals)
        watch._scan()
        return watch

    def _cron_calls(self, **context):
        """Run ``_cron_process`` once and return the progress it reported."""
        reported = []

        def commit(model, processed=0, *, remaining=None, deactivate=False):
            if remaining is not None:
                reported.append((processed, remaining))
            return float("inf")

        with patch.object(type(self.env["ir.cron"]), "_commit_progress", commit):
            self.env["ai.translation.unit"].with_context(**context)._cron_process()
        return reported

    def _gate(self):
        """Replace the pipeline by one that waits for an event; returns the event."""
        release = threading.Event()
        self.addCleanup(release.set)
        real = pipeline.process

        def gated(*args, **kwargs):
            release.wait(10)
            return real(*args, **kwargs)

        patcher = patch("odoo.addons.ai_translation_consensus.models.ai_translation_unit.pipeline.process", gated)
        patcher.start()
        self.addCleanup(patcher.stop)
        return release

    def test_cron_stores_what_the_providers_finished_and_reports_what_is_left(self):
        watch = self._cron_setup()
        self.assertEqual(self._cron_calls(), [(3, 0)])
        self.assertEqual(self._cron_calls(), [(0, 0)])
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"consensus"})

    def test_cron_runs_groups_side_by_side(self):
        ScriptedChat.barrier = threading.Barrier(3)
        self.addCleanup(setattr, ScriptedChat, "barrier", None)
        self.env["ai.translation.watch"].search([]).write({"auto_scan": False})
        names = ["Alpha", "Beta", "Gamma"]
        cats = self.env["res.partner.category"].create([{"name": n} for n in names])
        self.table = {n: n + "-de" for n in names}
        self._provider("gate", barrier=True)
        self._provider("two")
        watch = self._watch(domain="[('id', 'in', %s)]" % cats.ids, auto_scan=True, batch_size=1,
                            parallel_groups=3)
        watch._scan()
        # the barrier only opens when three groups wait in the provider at once
        self.assertEqual(self._cron_calls(), [(3, 0)])
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"consensus"})
        self.assertEqual(sorted(watch.unit_ids.mapped("final_text")), ["Alpha-de", "Beta-de", "Gamma-de"])

    def test_cron_takes_the_rules_in_turns(self):
        first = self._cron_setup(count=3, parallel_groups=2)
        cats = self.env["res.partner.category"].create([{"name": "Delta"}, {"name": "Epsilon"}])
        self.table.update({"Delta": "Delta-de", "Epsilon": "Epsilon-de"})
        second = self._watch(domain="[('id', 'in', %s)]" % cats.ids, auto_scan=True, batch_size=1,
                              parallel_groups=2)
        second._scan()
        Unit = self.env["ai.translation.unit"]
        submitted = []
        cron = self.env.ref("ai_translation_consensus.ir_cron_ai_translation")
        with patch.object(type(Unit), "_cron_submit", lambda model, job, db: submitted.append(job["watch"])), \
                patch.object(type(self.env["ir.cron"]), "_commit_progress", lambda *args, **kwargs: 0):
            started, _settled, queued = Unit._cron_start(cron, self.env.cr.dbname)
        self.assertEqual((started, queued), (2, 3))
        self.assertEqual(submitted, [first, second])

    def test_groups_keep_running_between_cron_calls(self):
        watch = self._cron_setup()
        release = self._gate()
        with patch.object(unit_module, "CRON_JOB_SECONDS", 0.3):
            self.assertEqual(self._cron_calls(), [(0, 3)])
            self.assertEqual(len(unit_module._INFLIGHT), 1)
            self.assertEqual(set(watch.unit_ids.mapped("state")), {"new"})
            # a second call while the provider is still busy neither stores nor doubles the group
            self.assertEqual(self._cron_calls(), [(0, 3)])
            self.assertEqual(len(unit_module._INFLIGHT), 1)
            release.set()
            unit_module._INFLIGHT[0]["future"].result(timeout=5)
            reported = self._cron_calls()
        self.assertGreaterEqual(reported[0][0], 1)
        self.assertGreaterEqual(len(watch.unit_ids.filtered(lambda u: u.state == "consensus")), 1)

    def test_threads_get_a_small_stack_and_it_is_restored(self):
        self._cron_setup(parallel_groups=1)
        before = threading.stack_size()
        seen = []
        executor = unit_module._executor()
        real_submit = executor.submit

        def spy(*args, **kwargs):
            seen.append(threading.stack_size())
            return real_submit(*args, **kwargs)

        with patch.object(executor, "submit", spy):
            self._cron_calls()
        self.assertEqual(seen[:1], [1024 * 1024])
        self.assertEqual(threading.stack_size(), before)

    def test_a_hanging_provider_is_abandoned_at_the_deadline(self):
        watch = self._cron_setup(parallel_groups=1)
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(*args, **kwargs):
            release.wait(10)
            raise RuntimeError("late")

        Unit = self.env["ai.translation.unit"]
        groups = Unit._group_units(watch.unit_ids, 1)[:2]
        started = time.monotonic()
        with patch("odoo.addons.ai_translation_consensus.models.ai_translation_unit.pipeline.process", hang):
            handled = Unit._process_groups_parallel(groups, deadline=time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(handled, 0)
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"new"})

    def test_a_group_waiting_too_long_is_given_up_and_left_alone_for_a_while(self):
        watch = self._cron_setup()
        self._gate()
        with patch.object(unit_module, "CRON_JOB_SECONDS", 0.3):
            self._cron_calls()
            self.assertEqual(len(unit_module._INFLIGHT), 1)
            unit_module._INFLIGHT[0]["started"] -= unit_module.INFLIGHT_MAX_SECONDS + 1
            reported = self._cron_calls()
        given_up = watch.unit_ids.filtered(lambda u: u.state == "new")
        self.assertEqual(len(given_up), 3)
        # the given-up group is not called again at once, the others are started
        self.assertEqual(len(unit_module._INFLIGHT), 1)
        self.assertEqual(reported, [(0, 2)])
        self.assertEqual(len(unit_module._COOLDOWN), 1)

    def test_malloc_arenas_are_limited_once(self):
        from ..models.ai_translation_unit import _limit_malloc_arenas
        self.assertEqual(_limit_malloc_arenas(), _limit_malloc_arenas())

    def test_a_failing_group_does_not_stop_the_others_and_is_not_repeated_at_once(self):
        watch = self._cron_setup(parallel_groups=3)
        real = pipeline.process
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("bug")
            return real(*args, **kwargs)

        with patch("odoo.addons.ai_translation_consensus.models.ai_translation_unit.pipeline.process", flaky):
            self.assertEqual(self._cron_calls(), [(2, 0)])
        self.assertEqual(len(calls), 3)
        self.assertEqual(sorted(watch.unit_ids.mapped("state")), ["consensus", "consensus", "new"])

    def test_cron_stops_at_the_quota_of_the_interval(self):
        watch = self._cron_setup(max_units_per_run=2)
        self.assertEqual(self._cron_calls(), [(2, 0)])
        self.assertEqual(self._cron_calls(), [(0, 0)])
        self.assertEqual(sorted(watch.unit_ids.mapped("state")), ["consensus", "consensus", "new"])

    def test_cron_without_provider_stops_instead_of_looping(self):
        self.env["ai.translation.provider"].search([]).write({"active": False})
        self.env["ai.translation.watch"].search([]).write({"auto_scan": False})
        watch = self._watch(auto_scan=True)
        watch._scan()
        self.assertEqual(self._cron_calls(), [(0, 0)])
        self.assertEqual(watch.unit_ids.state, "new")

    def test_cron_job_ends_itself_after_its_time_budget(self):
        watch = self._cron_setup()
        started_long_ago = time.monotonic() - 100 + MIN_TIME_PER_JOB
        self.assertEqual(self._cron_calls(cron_end_time=started_long_ago), [(0, 3)])
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"new"})
        self.assertFalse(unit_module._INFLIGHT)

    def test_cron_scans_only_while_its_time_budget_lasts(self):
        self._watch(auto_scan=True)
        Watch = type(self.env["ai.translation.watch"])
        scans = []
        with patch.object(Watch, "_scan", lambda watch, full=False: scans.append(watch.id)), \
                patch.object(type(self.env["ir.cron"]), "_commit_progress", lambda *a, **k: 0), \
                patch.object(type(self.env["ai.translation.unit"]), "_cron_process", lambda unit: None):
            watch_module._LAST_SCAN.clear()
            self.addCleanup(watch_module._LAST_SCAN.clear)
            self.env["ai.translation.watch"].with_context(
                cron_end_time=time.monotonic() - 100 + MIN_TIME_PER_JOB)._cron_run()
            self.assertFalse(scans)
            self.env["ai.translation.watch"]._cron_run()
            self.assertEqual(set(scans), set(self.env["ai.translation.watch"].search([("auto_scan", "=", True)]).ids))
            self.assertTrue(scans)
            scans.clear()
            self.env["ai.translation.watch"]._cron_run()
            self.assertFalse(scans, "the next call within five minutes does not scan again")

    def test_only_managers_switch_the_schedule(self):
        Watch = self.env["ai.translation.watch"]
        cron = self.env.ref("ai_translation_consensus.ir_cron_ai_translation")
        with patch.object(type(self.env["res.users"]), "has_group", return_value=False):
            self.assertFalse(Watch.get_dashboard()["can_manage"])
            with self.assertRaises(UserError):
                Watch.dashboard_toggle_cron()
        before = cron.active
        self.assertTrue(Watch.get_dashboard()["can_manage"])
        self.assertEqual(Watch.dashboard_toggle_cron(), not before)

    def test_wizard_passes_auto_scan_on(self):
        self._provider("one")
        self._provider("two")
        wiz = self._wizard(auto_scan=True)
        wiz.step = "done"
        wiz.action_create()
        self.assertTrue(self.env["ai.translation.watch"].search([], order="id desc", limit=1).auto_scan)

    # ------------------------------------------------------------ bulk rules
    def test_bulk_groups_fill_up_to_the_character_limit_per_language(self):
        watch = self._cron_setup(count=4, target_lang_ids=[(6, 0, (self.de | self.fr).ids)])
        Unit = self.env["ai.translation.unit"]
        units = watch.unit_ids.sorted(lambda u: (u.lang, u.id))
        self.assertEqual(len(units), 8)
        cost = len("Alpha") + unit_module.BULK_ITEM_OVERHEAD
        groups = Unit._group_units(units, 1, bulk_chars=cost * 2 + 5)
        self.assertEqual([len(g) for g in groups], [2, 2, 2, 2])
        self.assertTrue(all(len(set(g.mapped("lang"))) == 1 for g in groups))
        # without bulk_chars the old grouping stays: one group per record and language
        self.assertEqual(len(Unit._group_units(units, 1)), 8)
        # a text longer than the limit gets a request of its own
        self.assertEqual([len(g) for g in Unit._group_units(units[:3], 1, bulk_chars=1)], [1, 1, 1])

    def test_bulk_rule_sends_the_texts_of_all_records_in_one_request(self):
        watch = self._cron_setup(count=4, bulk_chars=20000, parallel_groups=2)
        calls = []
        real = pipeline.process

        def spy(kind, src, tgt, items, *args, **kwargs):
            calls.append((items, kwargs["page_context"]))
            return real(kind, src, tgt, items, *args, **kwargs)

        with patch("odoo.addons.ai_translation_consensus.models.ai_translation_unit.pipeline.process", spy):
            self.assertEqual(self._cron_calls(), [(4, 0)])
        self.assertEqual(len(calls), 1)
        items, page_context = calls[0]
        self.assertEqual(sorted(i["text"] for i in items), ["Alpha", "Beta", "Delta", "Gamma"])
        self.assertEqual(page_context, "")
        self.assertTrue(all(i["label"] for i in items))
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"consensus"})
        self.assertEqual(sorted(watch.unit_ids.mapped("final_text")),
                         ["Alpha-de", "Beta-de", "Delta-de", "Gamma-de"])

    def test_bulk_rule_keeps_the_good_results_when_one_record_changed(self):
        watch = self._cron_setup(bulk_chars=20000)
        alpha = self.env["res.partner.category"].search([("name", "=", "Alpha")])
        alpha.name = "Alpha changed"
        self.assertEqual(self._cron_calls(), [(3, 0)])
        states = {u.source_text: u.state for u in watch.unit_ids}
        self.assertEqual(states, {"Alpha": "stale", "Beta": "consensus", "Gamma": "consensus"})

    def test_bulk_rule_asks_again_only_for_the_texts_without_proposal(self):
        watch = self._cron_setup(bulk_chars=20000)
        self.env["ai.translation.provider"].search([]).write({"extra_json": __import__("json").dumps(
            dict(table={"Alpha": "Alpha-de", "Beta": "Beta-de"}, good=["Alpha-de", "Beta-de"]))})
        requests = []
        real = pipeline.process

        def spy(kind, src, tgt, items, *args, **kwargs):
            requests.append(sorted(i["text"] for i in items))
            return real(kind, src, tgt, items, *args, **kwargs)

        with patch("odoo.addons.ai_translation_consensus.models.ai_translation_unit.pipeline.process", spy):
            self._cron_calls()
            gamma = watch.unit_ids.filtered(lambda u: u.source_text == "Gamma")
            self.assertEqual((gamma.state, gamma.attempts), ("new", 1))
            self.assertEqual(set((watch.unit_ids - gamma).mapped("state")), {"consensus"})
            self.assertEqual(len(unit_module._COOLDOWN), 1)       # not asked again at once
            for expected_attempts in (2, 2):
                unit_module._COOLDOWN.clear()
                self._cron_calls()
                self.assertEqual(gamma.attempts, expected_attempts)
        self.assertEqual(requests, [["Alpha", "Beta", "Gamma"], ["Gamma"], ["Gamma"]])
        self.assertEqual(gamma.state, "failed")                    # a human decides now

    def test_a_rule_without_bulk_does_not_repeat_failures(self):
        watch = self._cron_setup()
        self.env["ai.translation.provider"].search([]).write({"extra_json": __import__("json").dumps(
            dict(table={"Alpha": "Alpha-de"}, good=["Alpha-de"]))})
        self._cron_calls()
        self.assertEqual(sorted(watch.unit_ids.mapped("state")), ["consensus", "failed", "failed"])
        self.assertEqual(set(watch.unit_ids.mapped("attempts")), {0})

    def test_bulk_rule_gives_command_providers_time_for_big_requests(self):
        self._provider("one")
        watch = self._watch(bulk_chars=20000)
        translators, _reviewers = watch._pick_providers()
        self.assertTrue(translators)
        self.assertTrue(all(p.timeout >= unit_module.BULK_TIMEOUT for p in translators))
        watch.bulk_chars = 0
        self.assertTrue(all(p.timeout < unit_module.BULK_TIMEOUT for p in watch._pick_providers()[0]))
        with self.assertRaises(ValidationError):
            watch.bulk_chars = -1

    def test_bulk_group_may_wait_longer_than_a_small_one(self):
        watch = self._cron_setup(bulk_chars=20000)
        self._gate()
        with patch.object(unit_module, "CRON_JOB_SECONDS", 0.3):
            self._cron_calls()
            self.assertEqual(len(unit_module._INFLIGHT), 1)
            unit_module._INFLIGHT[0]["started"] -= unit_module.INFLIGHT_MAX_SECONDS + 1
            self._cron_calls()
            self.assertEqual(len(unit_module._INFLIGHT), 1, "still waiting, the bulk limit is longer")
            unit_module._INFLIGHT[0]["started"] -= unit_module.BULK_INFLIGHT_SECONDS
            self._cron_calls()
        self.assertEqual(set(watch.unit_ids.mapped("state")), {"new"})
