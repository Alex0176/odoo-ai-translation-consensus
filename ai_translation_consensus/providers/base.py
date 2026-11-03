# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from ..engine import prompts
from ..engine.languages import lang_name
from .http import ProviderError, throttle


class BaseProvider:
    """One configured provider. ``cfg`` keys: name, base_url, model, api_key,
    region, extra (dict), timeout, command."""

    kind = ""
    label = ""
    can_review = False
    needs_key = True
    local_by_default = False
    defaults = {}

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.get("name") or self.kind
        self.timeout = int(cfg.get("timeout") or 120)
        self.extra = cfg.get("extra") or {}

    # -- capabilities
    def supports(self, lang_code):
        return True

    def can_generate(self, kind):
        return True

    # -- work
    def generate(self, kind, src, tgt, items, glossary=(), instructions="", page_context=""):
        raise NotImplementedError

    def review(self, kind, src, tgt, items, glossary=(), instructions="", page_context="", round_no=1):
        raise ProviderError("%s cannot review" % self.label)

    def ping(self):
        res = self.generate("translate", "de_DE", "en_GB",
                            [{"id": "1", "text": "Guten Tag"}])
        return res.get("1", {}).get("text", "")

    # -- helpers
    @property
    def key(self):
        return self.cfg.get("api_key") or ""

    def _throttle(self):
        interval = self.extra.get("min_interval")
        if interval:
            throttle("%s|%s" % (self.name, self.cfg.get("base_url")), interval)

    def _need_key(self):
        if self.needs_key and not self.key:
            raise ProviderError("no API key configured")


class ChatProvider(BaseProvider):
    """Large language model: translates, proofreads and reviews through prompts."""

    can_review = True

    def _complete(self, system, user):
        raise NotImplementedError

    def generate(self, kind, src, tgt, items, glossary=(), instructions="", page_context=""):
        system, user = prompts.generate_prompt(
            kind, lang_name(src), lang_name(tgt), items, glossary, instructions, page_context)
        self._throttle()
        return prompts.parse_generate(self._complete(system, user), [i["id"] for i in items])

    def review(self, kind, src, tgt, items, glossary=(), instructions="", page_context="", round_no=1):
        system, user = prompts.review_prompt(
            kind, lang_name(src), lang_name(tgt), items, glossary, instructions, page_context, round_no)
        self._throttle()
        return prompts.parse_review(self._complete(system, user), [i["id"] for i in items])


class MTProvider(BaseProvider):
    """Classic machine translation engine: translates only, cannot review or proofread."""

    can_review = False
    html = True

    def can_generate(self, kind):
        return kind == "translate"

    def _translate(self, texts, src, tgt):
        raise NotImplementedError

    def generate(self, kind, src, tgt, items, glossary=(), instructions="", page_context=""):
        if kind != "translate":
            raise ProviderError("%s cannot proofread" % self.label)
        texts = [i["text"] for i in items]
        result = self._translate(texts, src, tgt)
        out = {}
        for item, text in zip(items, result):
            if isinstance(text, str) and text.strip():
                out[str(item["id"])] = {"text": text, "note": ""}
        return out
