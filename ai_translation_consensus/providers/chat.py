# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Chat models: OpenAI-compatible APIs, Anthropic, Google Gemini, Cohere, local CLI."""
import shlex
import subprocess
import tempfile

from .base import ChatProvider
from .http import ProviderError, call


class OpenAICompatible(ChatProvider):
    """Anything that speaks ``POST {base_url}/chat/completions``.

    OpenAI, Mistral, DeepSeek, xAI, Groq, Together, OpenRouter, Cerebras, Qwen,
    Moonshot, GitHub Models, Hugging Face, Ollama, LM Studio, vLLM, llama.cpp,
    Pollinations ... ``extra`` may contain ``body`` (merged into the request) and
    ``headers``.
    """

    kind = "openai"
    label = "OpenAI-compatible API"
    defaults = {"base_url": "https://api.openai.com/v1", "model": "gpt-4o"}

    def _complete(self, system, user):
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
    defaults = {"base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-5"}

    def _complete(self, system, user):
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
    defaults = {"base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.5-flash"}

    def _complete(self, system, user):
        self._need_key()
        base = (self.cfg.get("base_url") or self.defaults["base_url"]).rstrip("/")
        model = self.cfg.get("model") or self.defaults["model"]
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
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


class LocalCommand(ChatProvider):
    """Runs a command line tool that reads the prompt on stdin and prints the answer.

    This is the "no key, but approved" route: a CLI that is already signed in
    on the server (Claude Code, Ollama, ...). The arguments are split without a
    shell. Because it executes a program, it only works when the system
    parameter ``ai_translation_consensus.allow_command`` is ``True``.
    """

    kind = "command"
    label = "Local command line tool (stdin/stdout)"
    needs_key = False
    local_by_default = False
    defaults = {}

    def _complete(self, system, user):
        if not self.cfg.get("allow_command"):
            raise ProviderError("command providers are disabled "
                                "(system parameter ai_translation_consensus.allow_command)")
        cmd = shlex.split(self.cfg.get("command") or "")
        if not cmd:
            raise ProviderError("no command configured")
        prompt = system + "\n\n" + user
        try:
            with tempfile.TemporaryDirectory() as cwd:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                      timeout=self.timeout, cwd=cwd, check=False)
        except FileNotFoundError:
            raise ProviderError("command not found: %s" % cmd[0])
        except subprocess.TimeoutExpired:
            raise ProviderError("command timed out after %s s" % self.timeout)
        if proc.returncode != 0:
            raise ProviderError("exit code %s: %s" % (proc.returncode, (proc.stderr or "")[:200]))
        return proc.stdout
