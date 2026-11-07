# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Machine translation engines (translate only, no review)."""
import html as htmllib
import re
import urllib.parse

from ..engine.languages import base_code, iso_code
from .base import MTProvider
from .http import ProviderError, ProviderUnavailable, call

_NEEDS_HTML = ("<", "&")


def _is_html(texts):
    return any(any(m in t for m in _NEEDS_HTML) for t in texts)


class DeepL(MTProvider):
    kind = "deepl"
    label = "DeepL"
    defaults = {"base_url": ""}
    # languages DeepL translates into, as of writing; unsupported ones are skipped
    TARGETS = {"ar", "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hu", "id",
               "it", "ja", "ko", "lt", "lv", "nb", "nl", "pl", "pt", "ro", "ru", "sk", "sl",
               "sv", "tr", "uk", "zh"}

    def supports(self, lang_code):
        return base_code(lang_code) in self.TARGETS

    @staticmethod
    def _target(code):
        base = base_code(code)
        if base == "en":
            return "EN-US" if code == "en_US" else "EN-GB"
        if base == "pt":
            return "PT-BR" if code == "pt_BR" else "PT-PT"
        if base == "zh":
            return "ZH-HANT" if code in ("zh_TW", "zh_HK") else "ZH-HANS"
        return base.upper()

    def _translate(self, texts, src, tgt):
        self._need_key()
        base = (self.cfg.get("base_url") or
                ("https://api-free.deepl.com" if self.key.endswith(":fx") else "https://api.deepl.com")).rstrip("/")
        body = {"text": texts, "source_lang": base_code(src).upper(),
                "target_lang": self._target(tgt), "preserve_formatting": True}
        if _is_html(texts):
            body["tag_handling"] = "html"
        body.update(self.extra.get("body") or {})
        data = call("POST", base + "/v2/translate",
                    headers={"Authorization": "DeepL-Auth-Key " + self.key},
                    json_body=body, timeout=self.timeout, secrets=[self.key])
        try:
            return [t["text"] for t in data["translations"]]
        except (KeyError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])


class GoogleTranslate(MTProvider):
    kind = "google"
    label = "Google Cloud Translation"
    defaults = {"base_url": "https://translation.googleapis.com/language/translate/v2"}

    def _translate(self, texts, src, tgt):
        self._need_key()
        as_html = _is_html(texts)
        body = {"q": texts, "source": iso_code(src), "target": iso_code(tgt),
                "format": "html" if as_html else "text"}
        data = call("POST", self.cfg.get("base_url") or self.defaults["base_url"],
                    headers={"X-goog-api-key": self.key}, json_body=body,
                    timeout=self.timeout, secrets=[self.key])
        try:
            out = [t["translatedText"] for t in data["data"]["translations"]]
        except (KeyError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])
        return out if as_html else [htmllib.unescape(t) for t in out]


class AzureTranslator(MTProvider):
    kind = "azure"
    label = "Microsoft Translator (Azure)"
    defaults = {"base_url": "https://api.cognitive.microsofttranslator.com"}

    def _translate(self, texts, src, tgt):
        self._need_key()
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        params = {"api-version": "3.0", "from": iso_code(src), "to": iso_code(tgt),
                  "textType": "html" if _is_html(texts) else "plain"}
        headers = {"Ocp-Apim-Subscription-Key": self.key}
        if self.cfg.get("region"):
            headers["Ocp-Apim-Subscription-Region"] = self.cfg["region"]
        data = call("POST", base + "/translate", headers=headers, params=params,
                    json_body=[{"Text": t} for t in texts], timeout=self.timeout, secrets=[self.key])
        try:
            return [row["translations"][0]["text"] for row in data]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])


class LibreTranslate(MTProvider):
    kind = "libretranslate"
    label = "LibreTranslate (self-hosted, no key needed)"
    needs_key = False
    local_by_default = True
    defaults = {"base_url": "http://localhost:5000"}

    def _translate(self, texts, src, tgt):
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        body = {"q": texts, "source": iso_code(src), "target": iso_code(tgt),
                "format": "html" if _is_html(texts) else "text"}
        if self.key:
            body["api_key"] = self.key
        data = call("POST", base + "/translate", json_body=body, timeout=self.timeout,
                    secrets=[self.key])
        out = data.get("translatedText") if isinstance(data, dict) else None
        if isinstance(out, str):
            out = [out]
        if not isinstance(out, list) or len(out) != len(texts):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])
        return out


def _quota_wait(message):
    """Seconds until the free quota is back, from 'NEXT AVAILABLE IN 10 HOURS 30 MINUTES ...'."""
    match = re.search(r"NEXT AVAILABLE IN\s+(?:(\d+)\s+HOURS?)?\s*(?:(\d+)\s+MINUTES?)?",
                      str(message or ""), re.I)
    if not match or not (match.group(1) or match.group(2)):
        return None
    return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60


class MyMemory(MTProvider):
    kind = "mymemory"
    label = "MyMemory (free, no key, small daily quota)"
    needs_key = False
    html = False
    defaults = {"base_url": "https://api.mymemory.translated.net"}

    def _translate(self, texts, src, tgt):
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        out = []
        for text in texts:
            if len(text.encode("utf-8")) > 480:
                out.append(None)        # the service limits one request to 500 bytes
                continue
            params = {"q": text, "langpair": "%s|%s" % (iso_code(src), iso_code(tgt))}
            if self.extra.get("email"):
                params["de"] = self.extra["email"]
            if self.key:
                params["key"] = self.key
            try:
                data = call("GET", base + "/get", params=params, timeout=self.timeout, secrets=[self.key])
            except ProviderUnavailable as exc:
                if exc.wait is None:
                    exc.wait = _quota_wait(exc)
                raise
            try:
                translated = data["responseData"]["translatedText"]
                status = data.get("responseStatus")
            except (KeyError, TypeError, AttributeError):
                raise ProviderError("unexpected answer: %s" % str(data)[:150])
            quota = status in (429, "429")
            if isinstance(translated, str):
                quota = quota or "MYMEMORY WARNING" in translated.upper()
            if quota:
                raise ProviderUnavailable("quota used up: %s" % str(translated)[:120],
                                          _quota_wait(translated))
            if not isinstance(translated, str):
                raise ProviderError("unexpected answer: %s" % str(data)[:150])
            if status not in (200, "200"):
                raise ProviderError("error %s: %s" % (status, translated[:120]))
            out.append(htmllib.unescape(translated))
        return out


class Lingva(MTProvider):
    kind = "lingva"
    label = "Lingva Translate (public or self-hosted instance, no key)"
    needs_key = False
    html = False
    defaults = {"base_url": "https://lingva.ml"}

    def _translate(self, texts, src, tgt):
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        out = []
        for text in texts:
            url = "%s/api/v1/%s/%s/%s" % (base, iso_code(src), iso_code(tgt),
                                           urllib.parse.quote(text, safe=""))
            data = call("GET", url, timeout=self.timeout)
            if not isinstance(data, dict) or "translation" not in data:
                raise ProviderError("unexpected answer: %s" % str(data)[:150])
            out.append(data["translation"])
        return out
