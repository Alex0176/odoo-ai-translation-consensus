# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("standard", "post_install", "-at_install")
class TestWebsiteScope(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ai.translation.provider"].search([]).write({"active": False})
        cls.env["ai.translation.provider"].create({
            "name": "site test", "kind": "openai_nokey", "base_url": "http://127.0.0.1:9", "model": "m"})
        cls.website = cls.env["website"].search([], limit=1)
        cls.de = cls.env.ref("base.lang_de")
        cls.de.active = True

    def _wizard(self, **vals):
        values = {"scope": "website", "website_id": self.website.id,
                  "source_lang_id": self.env.ref("base.lang_en").id,
                  "target_lang_ids": [(6, 0, self.de.ids)], "scan_now": False}
        values.update(vals)
        return self.env["ai.translation.wizard"].create(values)

    def test_suggestion_covers_menus_and_views(self):
        wiz = self._wizard()
        wiz._onchange_site()
        models_ = wiz.site_model_ids.mapped("model")
        self.assertIn("website.menu", models_)
        self.assertIn("ir.ui.view", models_)
        self.assertNotIn("res.partner", models_)
        self.assertNotIn("payment.provider", models_)

    def test_blogs_are_covered_and_shop_needs_a_shop(self):
        from odoo.addons.ai_translation_consensus_website.models.website_scope import has_shop, site_models
        website = self.env["website"].create({"name": "Scope test"})
        self.env["blog.blog"].create({"name": "Scope blog", "website_id": website.id})
        names = [row[0] for row in site_models(self.env, website)]
        self.assertIn("blog.blog", names)
        self.assertFalse(has_shop(self.env, website))
        self.assertNotIn("product.template", names)
        self.env["website.menu"].create({"name": "Shop", "url": "/shop", "website_id": website.id,
                                         "parent_id": website.menu_id.id})
        self.assertTrue(has_shop(self.env, website))

    def test_unpublished_records_are_left_out(self):
        from odoo.addons.ai_translation_consensus_website.models.website_scope import site_domain
        domain = site_domain(self.env["blog.post"], self.website)
        self.assertIn(("is_published", "=", True), domain)

    def test_one_rule_per_kind_of_content(self):
        wiz = self._wizard()
        models_ = self.env["ir.model"].search([("model", "in", ["website.menu"])])
        wiz.site_model_ids = models_
        wiz.step = "done"
        action = wiz.action_create()
        self.assertEqual(action["tag"], "ai_translation_consensus.dashboard")
        watch = self.env["ai.translation.watch"].search([("model_id.model", "=", "website.menu")])
        self.assertEqual(len(watch), 1)
        self.assertTrue(watch.field_ids)
        self.assertFalse(watch.auto_apply)
        self.assertEqual(watch.target_lang_ids, self.de)
        self.assertEqual(safe_domain(watch), self.website.id)

    def test_website_scope_needs_a_choice(self):
        wiz = self._wizard()
        wiz.step = "what"
        with self.assertRaises(UserError):
            wiz.action_next()
        with self.assertRaises(UserError):
            wiz.action_create()

    def test_record_scope_is_unchanged(self):
        wiz = self.env["ai.translation.wizard"].create({})
        self.assertEqual(wiz.scope, "record")


def safe_domain(watch):
    for leaf in watch._eval_domain():
        if isinstance(leaf, tuple) and leaf[0] == "website_id" and leaf[2]:
            return leaf[2]
    return False


@tagged("standard", "post_install", "-at_install")
class TestJsonldTerms(TransactionCase):
    HTML = ('<html><body><script type="application/ld+json">'
            '{"@type": "Service", "name": "IT-Betreuung", "url": "https://x.example/IT-Betreuung", '
            '"knowsAbout": ["Firewall", "Gäste-WLAN"], "offers": {"description": "Rund um die Uhr"}}'
            '</script><script type="application/ld+json">{broken</script></body></html>')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fr = cls.env.ref("base.lang_fr")
        cls.fr.active = True
        cls.Term = cls.env["website.jsonld.term"]
        cls.Term.search([]).unlink()
        for source, french in (("IT-Betreuung", "Assistance informatique"), ("Gäste-WLAN", "Wi-Fi invités")):
            term = cls.Term.create({"source": source, "translation": source})
            term.with_context(lang="fr_FR").translation = french

    def test_texts_are_replaced_in_the_language_of_the_visitor(self):
        html = self.Term._translate_html(self.HTML, "fr_FR")
        self.assertIn('"name": "Assistance informatique"', html)
        self.assertIn("Wi-Fi invités", html)
        self.assertIn("Firewall", html)                                     # no term: stays
        self.assertIn("Rund um die Uhr", html)
        self.assertIn('"url": "https://x.example/IT-Betreuung"', html)      # URLs are never touched
        self.assertIn("{broken", html)                                      # broken block is left alone

    def test_language_without_translation_keeps_the_source(self):
        self.assertEqual(self.Term._translate_html(self.HTML, "de_DE"), self.HTML)

    def test_collect_finds_static_texts_of_views(self):
        arch = ('<t t-name="x"><script type="application/ld+json">{"name": "Alarmanlagen", '
                '"description": "Schutz <t t-out=\'x\'/>", "knowsAbout": ["Einbruchschutz"], '
                '"url": "https://x.example"}</script></t>')
        texts = self.Term._texts_in_arch(arch)
        self.assertEqual(texts, {"Alarmanlagen", "Einbruchschutz"})
