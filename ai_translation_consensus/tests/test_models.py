# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from .. import providers
from ..providers.base import ChatProvider
from ..providers.http import ProviderError


class ScriptedChat(ChatProvider):
    """Registers as kind 'scripted'; answers from ``cfg['extra']['script']``."""

    kind = "scripted"
    label = "Scripted (tests)"
    needs_key = False
    defaults = {}

    def generate(self, kind, src, tgt, items, glossary=(), instructions="", page_context=""):
        if self.extra.get("fail"):
            raise ProviderError("down")
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
