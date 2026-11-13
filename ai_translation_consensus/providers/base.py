# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import threading
import time

from ..engine import prompts
from ..engine.languages import lang_name
from . import documents
from .http import ProviderError, throttle

_USAGE_LOCK = threading.Lock()
_LOCAL = threading.local()


DOCUMENT_TOKENS = 1500    # rough token count of one page or image when a service reports none


def estimate_tokens(text):
    """Rough token count (about four characters each) when a service reports none."""
    return max(1, len(text or "") // 4)


class BaseProvider:
    """One configured provider. ``cfg`` keys: name, base_url, model, api_key,
    region, extra (dict), timeout, command."""

    kind = ""
    label = ""
    can_review = False
    can_read_documents = False      # multimodal: reads images and PDF files (``read_document``)
    needs_key = True
    local_by_default = False
    defaults = {}

    @classmethod
    def documents_capable(cls, cfg):
        """Whether a provider of this kind configured with ``cfg`` can read documents.

        Usually a property of the kind; the command line kind depends on the command (``{images}``)."""
        return bool(cls.can_read_documents)

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

    def read_document(self, system, user, files):
        """Answer ``user`` about the documents in ``files`` ``[{'mimetype', 'data_b64', 'name'}]``."""
        raise ProviderError("%s cannot read documents" % self.label)

    def ping(self):
        res = self.generate("translate", "de_DE", "en_GB",
                            [{"id": "1", "text": "Guten Tag"}])
        return res.get("1", {}).get("text", "")

    # -- usage (what this object consumed; read by the caller after the run)
    @property
    def usage(self):
        data = self.__dict__.get("_usage")
        if data is None:
            data = self.__dict__.setdefault("_usage", {
                "calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "chars": 0,
                "seconds": 0.0, "estimated": False})
        return data

    def _record(self, **delta):
        with _USAGE_LOCK:
            data = self.usage
            for key, value in delta.items():
                data[key] = (data[key] or value) if key == "estimated" else data[key] + value

    def _tokens(self, input_tokens, output_tokens):
        """Called by an adapter with the token counts the service reported."""
        _LOCAL.tokens = (int(input_tokens or 0), int(output_tokens or 0))

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

    def _complete_document(self, system, user, files):
        raise ProviderError("%s cannot read documents" % self.label)

    def _ask(self, system, user, files=None):
        """``_complete`` (with ``files``: ``_complete_document``) plus bookkeeping of calls, time and tokens."""
        _LOCAL.tokens = None
        start = time.monotonic()
        try:
            text = self._complete_document(system, user, files) if files else self._complete(system, user)
        except Exception:
            self._record(calls=1, errors=1, seconds=time.monotonic() - start)
            raise
        seconds = time.monotonic() - start
        reported = getattr(_LOCAL, "tokens", None)
        if reported:
            self._record(calls=1, input_tokens=reported[0], output_tokens=reported[1], seconds=seconds)
        else:
            self._record(calls=1, input_tokens=estimate_tokens(system + user) + DOCUMENT_TOKENS * len(files or ()),
                         output_tokens=estimate_tokens(text), seconds=seconds, estimated=True)
        return text

    def read_document(self, system, user, files):
        if not self.documents_capable(self.cfg):
            raise ProviderError("%s cannot read documents" % self.label)
        files = documents.check_files(files)
        self._throttle()
        return self._ask(system, user, files)

    def generate(self, kind, src, tgt, items, glossary=(), instructions="", page_context=""):
        system, user = prompts.generate_prompt(
            kind, lang_name(src), lang_name(tgt), items, glossary, instructions, page_context)
        self._throttle()
        return prompts.parse_generate(self._ask(system, user), [i["id"] for i in items])

    def review(self, kind, src, tgt, items, glossary=(), instructions="", page_context="", round_no=1):
        system, user = prompts.review_prompt(
            kind, lang_name(src), lang_name(tgt), items, glossary, instructions, page_context, round_no)
        self._throttle()
        return prompts.parse_review(self._ask(system, user), [i["id"] for i in items])


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
        start = time.monotonic()
        try:
            result = self._translate(texts, src, tgt)
        except Exception:
            self._record(calls=1, errors=1, seconds=time.monotonic() - start)
            raise
        self._record(calls=1, chars=sum(len(t) for t in texts), seconds=time.monotonic() - start)
        out = {}
        for item, text in zip(items, result):
            if isinstance(text, str) and text.strip():
                out[str(item["id"])] = {"text": text, "note": ""}
        return out
