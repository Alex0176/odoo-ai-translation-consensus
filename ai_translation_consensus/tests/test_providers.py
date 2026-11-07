# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import json
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from odoo.tests.common import BaseCase, tagged

from .. import providers
from ..providers import http
from ..providers.http import ProviderError, ProviderUnavailable

REPLY = '{"1": {"text": "Hallo Welt", "note": ""}}'


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload, headers=None):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or "null") if length else None
        url = urlparse(self.path)
        self.server.requests.append({
            "method": self.command, "path": unquote(url.path), "query": parse_qs(url.query),
            "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        path = unquote(url.path)
        if path.startswith("/fail401"):
            return self._send(401, {"error": "bad key secret-key-123"})
        if path.startswith("/fail503"):
            return self._send(503, {"error": "overloaded"})
        if path.startswith("/free402"):
            self.server.flaky -= 1
            if self.server.flaky >= 0:
                return self._send(402, {"error": {"code": "FREE_LIMIT", "message": "Retry after 39s"}})
            return self._send(200, {"choices": [{"message": {"content": REPLY}}]})
        if path.startswith("/flaky"):
            self.server.flaky -= 1
            if self.server.flaky >= 0:
                return self._send(429, {"error": "slow down"}, {"Retry-After": "0"})
            return self._send(200, {"choices": [{"message": {"content": REPLY}}]})
        if path.endswith("/chat/completions"):
            return self._send(200, {"choices": [{"message": {"content": REPLY}}],
                                    "usage": {"prompt_tokens": 120, "completion_tokens": 30}})
        if path.endswith("/messages"):
            return self._send(200, {"content": [{"type": "text", "text": REPLY}],
                                    "usage": {"input_tokens": 110, "output_tokens": 25}})
        if ":generateContent" in path:
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": REPLY}]}}],
                                    "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 20}})
        if path.endswith("/chat"):
            return self._send(200, {"message": {"content": [{"type": "text", "text": REPLY}]},
                                    "usage": {"tokens": {"input_tokens": 80, "output_tokens": 15}}})
        if path.endswith("/v2/translate"):
            return self._send(200, {"translations": [{"text": "Hallo Welt"} for _ in body["text"]]})
        if path.endswith("/v2"):          # google
            return self._send(200, {"data": {"translations": [
                {"translatedText": "Hallo &amp; Welt"} for _ in body["q"]]}})
        if path.endswith("/translate") and isinstance(body, list):     # azure
            return self._send(200, [{"translations": [{"text": "Hallo Welt"}]} for _ in body])
        if path.endswith("/translate"):                                   # libretranslate
            return self._send(200, {"translatedText": ["Hallo Welt" for _ in body["q"]]})
        if path.endswith("/get"):                                         # mymemory
            asked = self.server.requests[-1]["query"]["q"][0]
            if "nothing" in asked:
                return self._send(200, {"responseStatus": 403, "responseData": {"translatedText": None}})
            if "daily" in asked:
                return self._send(429, {"responseStatus": 429, "responseData": {
                    "translatedText": "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY. "
                                      "NEXT AVAILABLE IN  10 HOURS 30 MINUTES 5 SECONDS"}})
            if "quota" in asked:
                return self._send(200, {"responseStatus": 429,
                                        "responseData": {"translatedText": "MYMEMORY WARNING: LIMIT"}})
            return self._send(200, {"responseStatus": 200, "responseData": {"translatedText": "Hallo Welt"}})
        if path.startswith("/api/v1/"):                                   # lingva
            return self._send(200, {"translation": "Hallo Welt"})
        return self._send(404, {"error": "unknown"})

    do_GET = do_POST = _handle


@tagged("standard", "at_install")
class TestProviders(BaseCase):
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

    def translate(self, prov):
        return prov.generate("translate", "en_US", "de_DE", [{"id": "1", "text": "Hello world"}])

    def last(self):
        return self.server.requests[-1]

    def test_openai_compatible(self):
        prov = self.provider("openai", model="m1")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        req = self.last()
        self.assertEqual(req["path"], "/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer secret-key-123")
        self.assertEqual(req["body"]["model"], "m1")
        self.assertIn("Hello world", req["body"]["messages"][1]["content"])

    def test_usage_is_read_from_every_api(self):
        for kind, expected in (("openai", (120, 30)), ("anthropic", (110, 25)),
                               ("gemini", (90, 20)), ("cohere", (80, 15))):
            prov = self.provider(kind, model="m1")
            self.translate(prov)
            usage = prov.usage
            self.assertEqual((usage["input_tokens"], usage["output_tokens"]), expected, kind)
            self.assertEqual((usage["calls"], usage["estimated"]), (1, False), kind)

    def test_usage_is_estimated_when_the_service_reports_none(self):
        prov = self.provider("openai", model="m1", base_url=self.url + "/flaky")
        self.server.flaky = 0
        self.translate(prov)
        self.assertTrue(prov.usage["estimated"])
        self.assertGreater(prov.usage["input_tokens"], 0)

    def test_translation_engine_usage_counts_characters(self):
        prov = self.provider("libretranslate")
        self.translate(prov)
        self.assertEqual(prov.usage["calls"], 1)
        self.assertEqual(prov.usage["chars"], len("Hello world"))

    def test_openai_extra_body(self):
        prov = self.provider("openai", model="m1", extra={"body": {"temperature": 0.1}})
        self.translate(prov)
        self.assertEqual(self.last()["body"]["temperature"], 0.1)

    def test_keyless_openai_sends_no_authorization(self):
        prov = self.provider("openai_nokey", api_key="", model="llama")
        self.translate(prov)
        self.assertNotIn("authorization", self.last()["headers"])

    def test_anthropic(self):
        prov = self.provider("anthropic")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(self.last()["headers"]["x-api-key"], "secret-key-123")
        self.assertIn("system", self.last()["body"])

    def test_anthropic_without_key(self):
        with self.assertRaises(ProviderError):
            self.translate(self.provider("anthropic", api_key=""))
        self.assertFalse(self.server.requests)

    def test_gemini(self):
        prov = self.provider("gemini", model="g1")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(self.last()["path"], "/models/g1:generateContent")
        self.assertEqual(self.last()["headers"]["x-goog-api-key"], "secret-key-123")
        self.assertNotIn("secret-key-123", self.last()["path"])

    def test_cohere(self):
        self.assertEqual(self.translate(self.provider("cohere"))["1"]["text"], "Hallo Welt")

    def test_deepl(self):
        prov = self.provider("deepl")
        out = prov.generate("translate", "en_US", "pt_BR", [{"id": "1", "text": "<b>Hi</b>"}])
        self.assertEqual(out["1"]["text"], "Hallo Welt")
        self.assertEqual(self.last()["headers"]["authorization"], "DeepL-Auth-Key secret-key-123")
        self.assertEqual(self.last()["body"]["target_lang"], "PT-BR")
        self.assertEqual(self.last()["body"]["tag_handling"], "html")
        self.assertTrue(prov.supports("de_DE"))
        self.assertFalse(prov.supports("hi_IN"))

    def test_google_unescapes_plain_text(self):
        out = self.translate(self.provider("google", base_url=self.url + "/v2"))
        self.assertEqual(out["1"]["text"], "Hallo & Welt")
        self.assertEqual(self.last()["headers"]["x-goog-api-key"], "secret-key-123")

    def test_azure(self):
        prov = self.provider("azure", region="westeurope")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(self.last()["headers"]["ocp-apim-subscription-region"], "westeurope")
        self.assertEqual(self.last()["query"]["to"], ["de"])

    def test_libretranslate_without_key(self):
        prov = self.provider("libretranslate", api_key="")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertNotIn("api_key", self.last()["body"])

    def test_mymemory(self):
        prov = self.provider("mymemory", api_key="")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(self.last()["query"]["langpair"], ["en|de"])
        long = prov.generate("translate", "en_US", "de_DE", [{"id": "1", "text": "x" * 600}])
        self.assertEqual(long, {})
        with self.assertRaises(ProviderUnavailable):
            prov.generate("translate", "en_US", "de_DE", [{"id": "1", "text": "quota please"}])

    def test_mymemory_daily_quota_is_an_outage_that_says_when_it_ends(self):
        prov = self.provider("mymemory", api_key="")
        with self.assertRaises(ProviderUnavailable) as ctx:
            prov.generate("translate", "en_US", "de_DE", [{"id": "1", "text": "daily"}])
        self.assertEqual(ctx.exception.wait, 10 * 3600 + 30 * 60)
        self.assertIn("MYMEMORY WARNING", str(ctx.exception))

    def test_mymemory_answer_without_text_is_an_error_not_a_crash(self):
        prov = self.provider("mymemory", api_key="")
        with self.assertRaises(ProviderError) as ctx:
            prov.generate("translate", "en_US", "de_DE", [{"id": "1", "text": "nothing"}])
        self.assertNotIsInstance(ctx.exception, ProviderUnavailable)
        self.assertIn("unexpected answer", str(ctx.exception))

    def test_server_trouble_is_an_outage_but_a_bad_key_is_not(self):
        with self.assertRaises(ProviderUnavailable):
            self.translate(self.provider("openai", model="m", base_url=self.url + "/fail503"))
        self.server.flaky = 10
        with self.assertRaises(ProviderUnavailable):
            self.translate(self.provider("openai", model="m", base_url=self.url + "/flaky"))
        with self.assertRaises(ProviderError) as ctx:
            self.translate(self.provider("openai", model="m", base_url=self.url + "/fail401"))
        self.assertNotIsInstance(ctx.exception, ProviderUnavailable)
        with patch.object(http.requests, "request", side_effect=http.requests.ConnectionError("down")):
            with self.assertRaises(ProviderUnavailable):
                self.translate(self.provider("openai", model="m"))

    def test_lingva(self):
        prov = self.provider("lingva", api_key="")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(self.last()["path"], "/api/v1/en/de/Hello world")

    def test_translation_engines_cannot_proofread_or_review(self):
        prov = self.provider("deepl")
        self.assertFalse(prov.can_review)
        self.assertFalse(prov.can_generate("proofread"))
        with self.assertRaises(ProviderError):
            prov.generate("proofread", "de_DE", "de_DE", [{"id": "1", "text": "x"}])

    def test_error_never_leaks_the_key(self):
        prov = self.provider("openai", model="m", base_url=self.url + "/fail401")
        with self.assertRaises(ProviderError) as ctx:
            self.translate(prov)
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("secret-key-123", str(ctx.exception))

    def test_retry_on_rate_limit(self):
        self.server.flaky = 2
        prov = self.provider("openai", model="m", base_url=self.url + "/flaky")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(len(self.server.requests), 3)

    def test_free_tier_limit_waits_as_told(self):
        waits = []
        self.server.flaky = 1
        prov = self.provider("openai", model="m", base_url=self.url + "/free402")
        with patch.object(http, "_sleep", waits.append):
            self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        self.assertEqual(waits, [40.0])

    def test_min_interval_spaces_the_calls(self):
        waits, now = [], [100.0]
        prov = self.provider("openai", model="m", name="spaced", extra={"min_interval": 45})
        with patch.object(http, "_sleep", waits.append), patch.object(http, "_clock", lambda: now[0]):
            http._last_call.clear()
            self.translate(prov)
            now[0] += 10
            self.translate(prov)
        self.assertEqual(waits, [35.0])

    def test_local_command_is_off_by_default(self):
        prov = self.provider("command", command="echo hi")
        with self.assertRaises(ProviderError) as ctx:
            self.translate(prov)
        self.assertIn("disabled", str(ctx.exception))

    def test_local_command(self):
        script = "import sys; sys.stdin.read(); print(%r)" % REPLY
        cmd = shlex.join([sys.executable, "-c", script])
        prov = self.provider("command", command=cmd, allow_command=True, api_key="")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")
        broken = self.provider("command", command=shlex.join([sys.executable, "-c", "raise SystemExit(3)"]),
                               allow_command=True)
        with self.assertRaises(ProviderError):
            self.translate(broken)

    def _failing_command(self, code, stdout="", stderr="", **cfg):
        script = "import sys; sys.stdin.read(); sys.stdout.write(%r); sys.stderr.write(%r); sys.exit(%d)" % (
            stdout, stderr, code)
        cmd = shlex.join([sys.executable, "-c", script])
        return self.provider("command", command=cmd, allow_command=True, api_key="", **cfg)

    def test_local_command_failure_shows_the_reason_and_tells_outage_from_error(self):
        # the limit message of the Claude CLI is on stdout, stderr stays empty
        with self.assertRaises(ProviderUnavailable) as ctx:
            self.translate(self._failing_command(1, stdout="Claude AI usage limit reached|1760000000"))
        self.assertIn("usage limit reached", str(ctx.exception))
        # the Codex CLI prints a long banner first and the reason last
        banner = "OpenAI Codex v0.1\n--------\nworkdir: /x\n" + "model: m\n" * 60
        with self.assertRaises(ProviderUnavailable) as ctx:
            self.translate(self._failing_command(1, stderr=banner + "ERROR: You've hit your usage limit."))
        self.assertIn("hit your usage limit", str(ctx.exception))
        with self.assertRaises(ProviderUnavailable):                  # silence says nothing about the text
            self.translate(self._failing_command(1))
        with self.assertRaises(ProviderUnavailable):                  # killed: memory allocation, signal
            self.translate(self._failing_command(1, stderr="memory allocation of 8 bytes failed"))
        with self.assertRaises(ProviderUnavailable):
            self.translate(self.provider("command", allow_command=True, api_key="", command=shlex.join(
                [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGABRT)"])))
        with self.assertRaises(ProviderError) as ctx:                 # a real complaint stays an error
            self.translate(self._failing_command(2, stderr=banner + "error: unknown option --foo"))
        self.assertNotIsInstance(ctx.exception, ProviderUnavailable)
        self.assertIn("unknown option --foo", str(ctx.exception))

    def test_local_command_timeout_is_an_outage(self):
        prov = self.provider("command", allow_command=True, api_key="", timeout=1, command=shlex.join(
            [sys.executable, "-c", "import time; time.sleep(5)"]))
        with self.assertRaises(ProviderUnavailable):
            self.translate(prov)

    def test_local_command_prompt_as_argument(self):
        script = "import sys; assert sys.stdin.read() == ''; assert 'Hello' in sys.argv[1]; print(%r)" % REPLY
        cmd = shlex.join([sys.executable, "-c", script, "{prompt}"])
        prov = self.provider("command", command=cmd, allow_command=True, api_key="")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")

    def test_local_command_prompt_embedded_in_argument(self):
        script = "import sys; a = sys.argv[1]; assert a.startswith('No tools. ') and 'Hello' in a; print(%r)" % REPLY
        cmd = shlex.join([sys.executable, "-c", script, "No tools. {prompt}"])
        prov = self.provider("command", command=cmd, allow_command=True, api_key="")
        self.assertEqual(self.translate(prov)["1"]["text"], "Hallo Welt")

    def test_every_registered_kind_can_be_built(self):
        for kind in providers.REGISTRY:
            self.assertTrue(providers.build(kind, {"name": kind}).kind)
