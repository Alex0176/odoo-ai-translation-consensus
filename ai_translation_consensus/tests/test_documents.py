# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Reading documents (invoices) and the provider purposes. HTTP is mocked by a local test server."""
import base64
import importlib.util
import os
import shutil
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import BaseCase, TransactionCase, tagged

from .. import providers
from ..providers import documents, http
from ..providers.base import ChatProvider
from ..providers.http import ProviderError, ProviderUnavailable
from .test_providers import _Handler

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 20).decode()
MINI_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
            b"trailer<</Root 1 0 R/Size 4>>\n%%EOF\n")
PDF = base64.b64encode(MINI_PDF).decode()
FILES = [{"mimetype": "image/png", "data_b64": PNG, "name": "scan.png"}]


@tagged("standard", "at_install")
class TestReadDocument(BaseCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.requests = []
        cls.server.flaky = 0
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.server.requests.clear()
        patcher = patch.object(http, "_sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def provider(self, kind, **cfg):
        cfg.setdefault("name", kind)
        cfg.setdefault("api_key", "secret-key-123")
        cfg.setdefault("base_url", self.url)
        return providers.build(kind, cfg)

    def last(self):
        return self.server.requests[-1]

    def test_only_multimodal_kinds_read_documents(self):
        capable = {k for k, cls in providers.REGISTRY.items() if cls.can_read_documents}
        self.assertEqual(capable, {"openai", "openai_nokey", "anthropic", "gemini"})
        for kind in ("cohere", "command", "deepl", "google", "azure", "libretranslate", "mymemory", "lingva"):
            prov = self.provider(kind)
            self.assertFalse(prov.can_read_documents, kind)
            with self.assertRaises(ProviderError):
                prov.read_document("sys", "user", FILES)

    def test_anthropic_sends_document_and_image_blocks(self):
        prov = self.provider("anthropic", model="m1")
        files = FILES + [{"mimetype": "application/pdf", "data_b64": PDF, "name": "r.pdf"}]
        self.assertEqual(prov.read_document("sys", "read it", files), '{"1": {"text": "Hallo Welt", "note": ""}}')
        req = self.last()
        self.assertEqual(req["path"], "/messages")
        self.assertEqual(req["headers"]["x-api-key"], "secret-key-123")
        content = req["body"]["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["image", "document", "text"])
        self.assertEqual(content[0]["source"], {"type": "base64", "media_type": "image/png", "data": PNG})
        self.assertEqual(content[1]["source"]["media_type"], "application/pdf")
        self.assertEqual(content[2]["text"], "read it")
        self.assertEqual(req["body"]["system"], "sys")
        self.assertEqual((prov.usage["input_tokens"], prov.usage["output_tokens"], prov.usage["calls"]), (110, 25, 1))

    def test_openai_image_as_data_uri(self):
        prov = self.provider("openai", model="m1")
        prov.read_document("sys", "read it", FILES)
        content = self.last()["body"]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}})
        self.assertEqual(content[-1], {"type": "text", "text": "read it"})

    def test_openai_pdf_as_file_block_when_the_service_takes_it(self):
        prov = self.provider("openai", model="m1", extra={"pdf_mode": "file"})
        prov.read_document("sys", "read", [{"mimetype": "application/pdf", "data_b64": PDF, "name": "r.pdf"}])
        part = self.last()["body"]["messages"][1]["content"][0]
        self.assertEqual(part["type"], "file")
        self.assertEqual(part["file"]["filename"], "r.pdf")
        self.assertTrue(part["file"]["file_data"].startswith("data:application/pdf;base64,"))

    def test_the_pdf_mode_default_follows_the_host(self):
        self.assertTrue(self.provider("openai", base_url="https://api.openai.com/v1")._pdf_as_file())
        self.assertFalse(self.provider("openai", base_url="http://localhost:11434/v1")._pdf_as_file())
        self.assertFalse(self.provider("openai_nokey")._pdf_as_file())

    @patch.object(documents.shutil, "which", return_value=None)
    def test_pdf_pages_need_pdftoppm_when_the_service_takes_no_pdf(self, _which):
        prov = self.provider("openai", model="m1", extra={"pdf_mode": "images"})
        with self.assertRaises(ProviderError) as ctx:
            prov.read_document("sys", "read", [{"mimetype": "application/pdf", "data_b64": PDF, "name": "r.pdf"}])
        self.assertIn("pdftoppm", str(ctx.exception))
        self.assertEqual(prov.usage["errors"], 1)

    def test_openai_pdf_pages_become_png_images(self):
        if not shutil.which("pdftoppm"):
            self.skipTest("pdftoppm is not installed")
        prov = self.provider("openai", model="m1", extra={"pdf_mode": "images"})
        prov.read_document("sys", "read", [{"mimetype": "application/pdf", "data_b64": PDF, "name": "r.pdf"}])
        content = self.last()["body"]["messages"][1]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        url = content[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1])[:8], b"\x89PNG\r\n\x1a\n")

    def test_gemini_inline_data(self):
        prov = self.provider("gemini", model="m1")
        prov.read_document("sys", "read it", FILES)
        req = self.last()
        self.assertIn(":generateContent", req["path"])
        parts = req["body"]["contents"][0]["parts"]
        self.assertEqual(parts[0], {"inlineData": {"mimeType": "image/png", "data": PNG}})
        self.assertEqual(parts[1], {"text": "read it"})
        self.assertEqual(req["headers"]["x-goog-api-key"], "secret-key-123")

    def test_text_requests_are_unchanged(self):
        prov = self.provider("anthropic", model="m1")
        prov.generate("translate", "en_US", "de_DE", [{"id": "1", "text": "Hello"}])
        content = self.last()["body"]["messages"][0]["content"]
        self.assertIsInstance(content, str)

    def test_usage_is_estimated_per_page_when_nothing_is_reported(self):
        prov = self.provider("openai", model="m1")
        with patch.object(type(prov), "_complete_document", lambda s, a, b, c: "{}"):
            prov.read_document("sys", "user", FILES)
        usage = prov.usage
        self.assertTrue(usage["estimated"])
        self.assertGreaterEqual(usage["input_tokens"], documents_tokens())

    def test_failures_are_counted_and_never_leak_the_key(self):
        prov = self.provider("anthropic", base_url=self.url + "/fail401")
        with self.assertRaises(ProviderError) as ctx:
            prov.read_document("sys", "user", FILES)
        self.assertNotIn("secret-key-123", str(ctx.exception))
        self.assertEqual((prov.usage["calls"], prov.usage["errors"]), (1, 1))
        prov = self.provider("anthropic", base_url=self.url + "/fail503")
        with self.assertRaises(ProviderUnavailable):
            prov.read_document("sys", "user", FILES)

    def test_files_are_checked(self):
        prov = self.provider("anthropic")
        for files in ([], [{"mimetype": "image/heic", "data_b64": PNG}], [{"mimetype": "image/png"}],
                      FILES * (documents.MAX_FILES + 1)):
            with self.assertRaises(ProviderError):
                prov.read_document("s", "u", files)
        self.assertFalse(self.server.requests)


def documents_tokens():
    from ..providers.base import DOCUMENT_TOKENS
    return DOCUMENT_TOKENS


class ScriptedReader(ChatProvider):
    kind = "scripted_reader"
    label = "Scripted reader (tests)"
    needs_key = False
    can_read_documents = True
    defaults = {}
    calls = []

    def _complete_document(self, system, user, files):
        ScriptedReader.calls.append((self.name, files))
        if self.extra.get("outage"):
            raise ProviderUnavailable("usage limit reached", 600)
        if self.extra.get("fail"):
            raise ProviderError("broken")
        self._tokens(1000, 200)
        return self.extra.get("answer", "{}")


@tagged("standard", "at_install")
class TestPurposes(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        providers.REGISTRY["scripted_reader"] = ScriptedReader
        cls.addClassCleanup(providers.REGISTRY.pop, "scripted_reader", None)
        cls.env["ai.translation.provider"].search([]).write({"active": False})

    def purpose(self, code):
        return self.env["ai.translation.purpose"].search([("code", "=", code)])

    def setUp(self):
        super().setUp()
        ScriptedReader.calls.clear()
        self.Provider = self.env["ai.translation.provider"]

    def assertRaisesKeep(self, exc_type, func, *args):
        """Like assertRaises but without the savepoint rollback: the code under test keeps its bookkeeping
        when the service fails (the caller of the real code does not roll back either)."""
        try:
            func(*args)
        except exc_type:
            return
        self.fail("%s not raised" % exc_type.__name__)

    def reader(self, name="reader", codes=("invoice_manual",), **vals):
        vals.setdefault("kind", "scripted_reader")
        extra = vals.pop("extra", {})
        return self.Provider.create(dict(
            name=name, extra_json=__import__("json").dumps(extra),
            purpose_ids=[(6, 0, self.env["ai.translation.purpose"].search([("code", "in", list(codes))]).ids)],
            **vals))

    def test_purposes_are_shipped(self):
        codes = self.env["ai.translation.purpose"].search([]).mapped("code")
        self.assertEqual(set(codes), {"translate", "invoice_manual", "invoice_auto", "other"})
        self.assertEqual(sorted(self.env["ai.translation.purpose"].search([("is_document", "=", True)]).mapped("code")),
                         ["invoice_auto", "invoice_manual"])
        self.assertTrue(self.env.ref("ai_translation_consensus.purpose_invoice_manual").is_document)
        self.assertFalse(self.env.ref("ai_translation_consensus.purpose_translate").is_document)

    def test_new_providers_translate_by_default(self):
        prov = self.Provider.create({"name": "plain", "kind": "openai_nokey"})
        self.assertEqual(prov.purpose_ids.mapped("code"), ["translate"])

    def test_every_shipped_preset_translates(self):
        presets = self.Provider.with_context(active_test=False).search([])
        self.assertTrue(presets)
        for prov in presets:
            self.assertIn("translate", prov.purpose_ids.mapped("code"), prov.name)

    def test_migration_gives_every_existing_provider_the_translation_purpose(self):
        old = self.Provider.create({"name": "old", "kind": "openai_nokey", "active": False})
        mixed = self.reader("keeps", codes=("invoice_auto",))
        self.env.cr.execute("DELETE FROM ai_translation_provider_purpose_rel WHERE provider_id = %s", [old.id])
        self.env.invalidate_all()
        self.assertFalse(old.purpose_ids)
        path = os.path.join(os.path.dirname(__file__), "..", "migrations", "19.0.1.3.0", "post-migration.py")
        spec = importlib.util.spec_from_file_location("post_migration_1_3_0", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "19.0.1.2.1")
        module.migrate(self.env.cr, "19.0.1.2.1")        # twice: harmless
        self.env.invalidate_all()
        self.assertEqual(old.purpose_ids.mapped("code"), ["translate"])
        self.assertEqual(mixed.purpose_ids.mapped("code"), ["invoice_auto"])    # untouched

    def test_translation_runs_only_use_providers_with_the_purpose(self):
        translator = self.Provider.create({"name": "tr", "kind": "openai_nokey", "api_key": "x"})
        invoices_only = self.reader("inv", kind="openai_nokey", is_local=True)
        self.assertEqual(invoices_only.purpose_ids.mapped("code"), ["invoice_manual"])
        watch = self.env["ai.translation.watch"].create({
            "name": "w", "model_id": self.env["ir.model"]._get("res.partner.category").id,
            "domain": "[]", "field_ids": [(6, 0, self.env["ir.model.fields"]._get("res.partner.category", "name").ids)],
            "source_lang_id": self.env.ref("base.lang_en").id})
        pool = watch._translation_pool()
        self.assertEqual(pool, translator)
        watch.provider_ids = invoices_only | translator
        self.assertEqual(watch._translation_pool(), translator)
        watch.arbiter_id = invoices_only
        self.assertIsNone(watch._pick_arbiter())

    def test_invoice_purposes_need_a_provider_that_reads_documents(self):
        with self.assertRaises(ValidationError):
            self.Provider.create({"name": "mt", "kind": "deepl", "purpose_ids": [
                (6, 0, self.purpose("invoice_manual").ids)]})
        prov = self.Provider.create({"name": "mt", "kind": "deepl"})
        with self.assertRaises(ValidationError):
            prov.purpose_ids |= self.purpose("invoice_auto")
        self.reader("ok", kind="anthropic")             # a capable kind is fine

    def test_data_protection_switch(self):
        external = self.reader("external")
        local = self.reader("local", is_local=True)
        self.assertEqual(self.Provider._pick_for_documents("invoice_manual"), local)
        self.assertIn("data protection", external._documents_blocker("invoice_manual"))
        external.allow_documents_external = True
        self.assertEqual(self.Provider._pick_for_documents("invoice_manual"), external | local)
        self.assertFalse(self.Provider.create({"name": "x", "kind": "openai_nokey"}).allow_documents_external)

    def test_selection_follows_the_purpose_the_sequence_and_the_pause(self):
        first = self.reader("first", is_local=True, sequence=1)
        second = self.reader("second", codes=("invoice_manual", "invoice_auto"), is_local=True, sequence=2)
        self.reader("translator", codes=("translate",), is_local=True, sequence=0)
        self.assertEqual(self.Provider._pick_for_documents("invoice_manual"), first | second)
        self.assertEqual(self.Provider._pick_for_documents("invoice_auto"), second)
        first.write({"paused_until": "2999-01-01 00:00:00", "pause_reason": "quota"})
        self.assertEqual(self.Provider._pick_for_documents("invoice_manual"), second)
        self.assertEqual(self.Provider._pick_for_documents("invoice_manual", respect_pause=False), first | second)
        self.assertEqual(self.Provider._pick_for_documents("other"), self.Provider)

    def test_a_key_is_needed_when_the_kind_needs_one(self):
        prov = self.reader("keyed", kind="anthropic", is_local=True)
        self.assertEqual(prov._documents_blocker("invoice_manual"), "no API key")
        prov.api_key = "k"
        self.assertEqual(prov._documents_blocker("invoice_manual"), "")

    def test_read_document_logs_usage_with_the_purpose_and_the_cost(self):
        prov = self.reader("local", is_local=True, price_input=2.0, price_output=10.0,
                           extra={"answer": '{"ok": 1}'})
        text = prov._read_document("sys", "user", FILES, "invoice_manual")
        self.assertEqual(text, '{"ok": 1}')
        usage = self.env["ai.translation.usage"].search([("provider_id", "=", prov.id)])
        self.assertEqual((len(usage), usage.calls, usage.input_tokens, usage.units), (1, 1, 1000, 1))
        self.assertEqual(usage.purpose_id.code, "invoice_manual")
        self.assertFalse(usage.watch_id)
        self.assertAlmostEqual(usage.cost, (1000 * 2.0 + 200 * 10.0) / 1e6)

    def test_read_document_refuses_a_provider_that_may_not(self):
        external = self.reader("external")
        with self.assertRaises(ProviderError):
            external._read_document("s", "u", FILES, "invoice_manual")
        local = self.reader("local", is_local=True)
        with self.assertRaises(ProviderError):
            local._read_document("s", "u", FILES, "invoice_auto")        # purpose not set
        self.assertFalse(ScriptedReader.calls)

    def test_an_outage_pauses_the_provider_and_the_error_survives(self):
        prov = self.reader("down", is_local=True, extra={"outage": True})
        self.assertRaisesKeep(ProviderUnavailable, prov._read_document, "s", "u", FILES, "invoice_manual")
        self.assertTrue(prov.is_paused)
        self.assertEqual(self.Provider._pick_for_documents("invoice_manual"), self.Provider)
        usage = self.env["ai.translation.usage"].search([("provider_id", "=", prov.id)])
        self.assertEqual(usage.errors, 1)

    def test_repeated_errors_pause_after_the_streak_limit(self):
        prov = self.reader("flaky", is_local=True, extra={"fail": True})
        for _i in range(3):
            self.assertRaisesKeep(ProviderError, prov._read_document, "s", "u", FILES, "invoice_manual")
        self.assertTrue(prov.is_paused)

    def test_a_good_answer_lifts_a_pause_by_error_streak(self):
        prov = self.reader("ok", is_local=True, extra={"answer": "{}"})
        prov.error_streak = 2
        prov._read_document("s", "u", FILES, "invoice_manual")
        self.assertEqual(prov.error_streak, 0)

    def test_provider_search_by_ability(self):
        capable = self.Provider.search([("can_read_documents", "=", True)])
        self.assertTrue(all(p.kind in ("openai", "openai_nokey", "anthropic", "gemini", "scripted_reader")
                            for p in capable))
        others = self.Provider.with_context(active_test=False).search([("can_read_documents", "=", False)])
        self.assertFalse(others & capable)
