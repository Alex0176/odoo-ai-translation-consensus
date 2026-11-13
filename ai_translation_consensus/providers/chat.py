# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Chat models: OpenAI-compatible APIs, Anthropic, Google Gemini, Cohere, local CLI."""
import base64
import binascii
import os
import re
import shlex
import shutil
import subprocess
import tempfile

from urllib.parse import urlparse

from . import documents
from .base import ChatProvider
from .http import ProviderError, ProviderUnavailable, call


class OpenAICompatible(ChatProvider):
    """Anything that speaks ``POST {base_url}/chat/completions``.

    OpenAI, Mistral, DeepSeek, xAI, Groq, Together, OpenRouter, Cerebras, Qwen,
    Moonshot, GitHub Models, Hugging Face, Ollama, LM Studio, vLLM, llama.cpp,
    Pollinations ... ``extra`` may contain ``body`` (merged into the request) and
    ``headers``.
    """

    kind = "openai"
    label = "OpenAI-compatible API"
    can_read_documents = True       # needs a model that sees images (gpt-4o, pixtral, llava ...)
    defaults = {"base_url": "https://api.openai.com/v1", "model": "gpt-4o"}
    # Services that take a PDF as a "file" block; every other endpoint gets the pages as images.
    PDF_FILE_HOSTS = ("api.openai.com", "openrouter.ai")

    def _complete(self, system, user):
        return self._chat(system, user)

    def _pdf_as_file(self):
        mode = self.extra.get("pdf_mode")
        if mode in ("file", "images"):
            return mode == "file"
        host = urlparse(self.cfg.get("base_url") or self.defaults["base_url"]).hostname or ""
        return host in self.PDF_FILE_HOSTS

    def _complete_document(self, system, user, files):
        parts = []
        for item in files:
            if item["mimetype"] == documents.PDF_TYPE and self._pdf_as_file():
                parts.append({"type": "file", "file": {
                    "filename": item["name"] or "document.pdf",
                    "file_data": "data:application/pdf;base64," + item["data_b64"]}})
                continue
            pages = (documents.pdf_to_png(item["data_b64"]) if item["mimetype"] == documents.PDF_TYPE
                     else [item["data_b64"]])
            mimetype = "image/png" if item["mimetype"] == documents.PDF_TYPE else item["mimetype"]
            for page in pages:
                parts.append({"type": "image_url",
                              "image_url": {"url": "data:%s;base64,%s" % (mimetype, page)}})
        parts.append({"type": "text", "text": user})
        return self._chat(system, parts)

    def _chat(self, system, user):
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        if not self.cfg.get("model"):
            raise ProviderError("no model configured")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        headers.update(self.extra.get("headers") or {})
        body = {
            "model": self.cfg["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        body.update(self.extra.get("body") or {})
        data = call("POST", base + "/chat/completions", headers=headers, json_body=body,
                    timeout=self.timeout, secrets=[self.key])
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
            self._tokens(usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return content or ""


class NoKeyOpenAI(OpenAICompatible):
    kind = "openai_nokey"
    label = "OpenAI-compatible API without key (local or free endpoint)"
    needs_key = False
    defaults = {"base_url": "http://localhost:11434/v1", "model": "llama3.1"}


class Anthropic(ChatProvider):
    kind = "anthropic"
    label = "Anthropic Claude"
    can_read_documents = True
    defaults = {"base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-5"}

    def _complete(self, system, user):
        return self._messages(system, user)

    def _complete_document(self, system, user, files):
        blocks = []
        for item in files:
            kind = "document" if item["mimetype"] == documents.PDF_TYPE else "image"
            blocks.append({"type": kind, "source": {"type": "base64", "media_type": item["mimetype"],
                                                    "data": item["data_b64"]}})
        blocks.append({"type": "text", "text": user})
        return self._messages(system, blocks)

    def _messages(self, system, user):
        self._need_key()
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        body = {
            "model": self.cfg.get("model") or self.defaults["model"],
            "max_tokens": int(self.extra.get("max_tokens", 8192)),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        body.update(self.extra.get("body") or {})
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        data = call("POST", base + "/messages", headers=headers, json_body=body,
                    timeout=self.timeout, secrets=[self.key])
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and usage.get("input_tokens") is not None:
            self._tokens(usage.get("input_tokens"), usage.get("output_tokens"))
        try:
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])


class Gemini(ChatProvider):
    kind = "gemini"
    label = "Google Gemini"
    can_read_documents = True
    defaults = {"base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.5-flash"}

    def _complete(self, system, user):
        return self._generate(system, [{"text": user}])

    def _complete_document(self, system, user, files):
        parts = [{"inlineData": {"mimeType": item["mimetype"], "data": item["data_b64"]}} for item in files]
        parts.append({"text": user})
        return self._generate(system, parts)

    def _generate(self, system, parts):
        self._need_key()
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        model = self.cfg.get("model") or self.defaults["model"]
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        body.update(self.extra.get("body") or {})
        data = call("POST", "%s/models/%s:generateContent" % (base, model),
                    headers={"x-goog-api-key": self.key, "content-type": "application/json"},
                    json_body=body, timeout=self.timeout, secrets=[self.key])
        meta = data.get("usageMetadata") if isinstance(data, dict) else None
        if isinstance(meta, dict) and meta.get("promptTokenCount") is not None:
            self._tokens(meta.get("promptTokenCount"), meta.get("candidatesTokenCount"))
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])


class Cohere(ChatProvider):
    kind = "cohere"
    label = "Cohere"
    defaults = {"base_url": "https://api.cohere.com/v2", "model": "command-a-03-2025"}

    def _complete(self, system, user):
        self._need_key()
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        body = {
            "model": self.cfg.get("model") or self.defaults["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        body.update(self.extra.get("body") or {})
        data = call("POST", base + "/chat",
                    headers={"Authorization": "Bearer " + self.key, "content-type": "application/json"},
                    json_body=body, timeout=self.timeout, secrets=[self.key])
        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        tokens = usage.get("tokens") or usage.get("billed_units") or {}
        if tokens.get("input_tokens") is not None:
            self._tokens(tokens.get("input_tokens"), tokens.get("output_tokens"))
        try:
            return "".join(p.get("text", "") for p in data["message"]["content"])
        except (KeyError, TypeError):
            raise ProviderError("unexpected answer: %s" % str(data)[:150])


# What a signed-in CLI prints when its subscription or the service is not available right now.
_CLI_OUTAGE = re.compile(
    r"usage limit|limit reached|hit your .*limit|rate.?limit|too many requests|quota|overloaded|"
    r"over capacity|try again|credit balance|insufficient|resets? (?:at|in)|\b(?:429|500|502|503|504|529)\b|"
    r"unavailable|temporar|connection|network|timed? ?out|stream (?:disconnected|error)|"
    r"memory allocation|out of memory|cannot allocate|not logged in|please run /login|"
    r"authentication|unauthorized|log ?in", re.I)


CMD_PDF_DPI = 150             # PDF pages handed to a command line tool
LIFT_MEMORY_LIMIT = 'ulimit -S -v "$(ulimit -H -v)" 2>/dev/null; exec "$@"'


def _cli_failure(returncode, stdout, stderr):
    """The error for a CLI that exited non-zero. Banners come first, the reason comes last."""
    text = " | ".join(part.strip()[-300:] for part in (stderr, stdout) if part and part.strip())
    text = re.sub(r"\s+", " ", text)
    message = "exit code %s: %s" % (returncode, text)
    # A signal (negative code) or silence says nothing about the text; so do limits and outages.
    if returncode < 0 or not text or _CLI_OUTAGE.search(text):
        return ProviderUnavailable(message)
    return ProviderError(message)


class LocalCommand(ChatProvider):
    """Runs a command line tool that reads the prompt on stdin and prints the answer.

    This is the "no key, but approved" route: a CLI that is already signed in
    on the server (Claude Code, Ollama, ...). The arguments are split without a
    shell. A tool that takes the prompt as an argument instead (Antigravity ``agy -p``)
    gets it in place of ``{prompt}`` inside an argument (it may carry a fixed lead-in text); stdin stays empty then.
    A tool that can look at images gets documents through ``{images}``: every file
    is written to a temporary directory (PDF pages as PNG, see ``documents.pdf_to_png``) and the
    placeholder is replaced by the file paths, one argument each (an argument that only contains the
    placeholder is replaced by all paths; ``--image={images}`` is repeated per path). Example for Codex
    (prompt on stdin, ``-`` before ``-i`` because ``-i`` takes any number of values)::

        codex exec --sandbox read-only --skip-git-repo-check --ephemeral - -i {images}

    A command without ``{images}`` cannot read documents.
    Because it executes a program, it only works when the system
    parameter ``ai_translation_consensus.allow_command`` is ``True``.
    """

    kind = "command"
    label = "Local command line tool (stdin/stdout)"
    needs_key = False
    local_by_default = False
    defaults = {}
    IMAGES = "{images}"
    IMAGE_FLAGS = ("-i", "--image")

    @classmethod
    def documents_capable(cls, cfg):
        return cls.IMAGES in (cfg.get("command") or "")

    def _complete(self, system, user):
        return self._run(system, user, None)

    def _complete_document(self, system, user, files):
        return self._run(system, user, files)

    def _expand_images(self, cmd, paths):
        """``cmd`` with the placeholder replaced by ``paths`` (own temporary files); no shell is involved."""
        out = []
        for arg in cmd:
            if self.IMAGES not in arg:
                out.append(arg)
            elif arg == self.IMAGES:
                out.extend(paths)
            else:
                out.extend(arg.replace(self.IMAGES, path) for path in paths)
        return out

    def _drop_images(self, cmd):
        """Text request with a command made for documents: leave out the placeholder (and its flag)."""
        out = []
        for arg in cmd:
            if arg == self.IMAGES:
                if out and out[-1] in self.IMAGE_FLAGS:
                    out.pop()
            elif self.IMAGES not in arg:
                out.append(arg)
        return out

    def _write_images(self, tmp, files):
        """Paths of image files in ``tmp`` for the documents (PDF pages as PNG at 150 dpi)."""
        paths = []
        for n, item in enumerate(files, 1):
            if item["mimetype"] == documents.PDF_TYPE:
                pages = documents.pdf_to_png(item["data_b64"], pages=documents.PDF_PAGES, dpi=CMD_PDF_DPI)
                blobs = [(".png", base64.b64decode(page)) for page in pages]
            else:
                try:
                    raw = base64.b64decode(item["data_b64"], validate=True)
                except (binascii.Error, ValueError):
                    raise ProviderError("the document is not valid base64")
                blobs = [(documents.EXTENSIONS.get(item["mimetype"], ".bin"), raw)]
            for page_no, (ext, raw) in enumerate(blobs, 1):
                path = os.path.join(tmp, "doc%02d-%02d%s" % (n, page_no, ext))
                with open(path, "wb") as handle:
                    handle.write(raw)
                paths.append(path)
        return paths

    def _run(self, system, user, files):
        if not self.cfg.get("allow_command"):
            raise ProviderError("command providers are disabled "
                                "(system parameter ai_translation_consensus.allow_command)")
        cmd = shlex.split(self.cfg.get("command") or "")
        if not cmd:
            raise ProviderError("no command configured")
        if not shutil.which(cmd[0]):
            raise ProviderError("command not found: %s" % cmd[0])
        if files and not any(self.IMAGES in arg for arg in cmd):
            raise ProviderError("the command has no %s placeholder, so it cannot read documents" % self.IMAGES)
        prompt = system + "\n\n" + user
        as_argument = any("{prompt}" in arg for arg in cmd)
        try:
            with tempfile.TemporaryDirectory() as cwd:
                if files:
                    cmd = self._expand_images(cmd, self._write_images(cwd, files))
                else:
                    cmd = self._drop_images(cmd)
                if as_argument:
                    cmd = [arg.replace("{prompt}", prompt) for arg in cmd]
                # Odoo workers cap the address space (limit_memory_hard); Go based tools such as agy
                # reserve far more virtual memory and die with "out of memory", so lift the soft limit.
                cmd = ["sh", "-c", LIFT_MEMORY_LIMIT, "sh"] + cmd
                proc = subprocess.run(cmd, input=None if as_argument else prompt,
                                      stdin=subprocess.DEVNULL if as_argument else None,
                                      capture_output=True, text=True,
                                      timeout=self.timeout, cwd=cwd, check=False)
        except FileNotFoundError:
            raise ProviderError("command not found: %s" % cmd[0])
        except subprocess.TimeoutExpired:
            raise ProviderUnavailable("command timed out after %s s" % self.timeout)
        if proc.returncode != 0:
            raise _cli_failure(proc.returncode, proc.stdout, proc.stderr)
        return proc.stdout
