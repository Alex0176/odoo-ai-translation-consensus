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
        table = self.extra.get("table", {})
        ScriptedChat.last_context = page_context
        ScriptedChat.last_glossary = list(glossary)
        return {str(i["id"]): {"text": table.get(i["text"], i["text"]), "note": ""}
                for i in items if i["text"] in table}

    def review(self, kind, src, tgt, items, glossary=(), instructions="", page_context="", round_no=1):
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
