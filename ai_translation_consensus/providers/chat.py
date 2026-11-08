# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Chat models: OpenAI-compatible APIs, Anthropic, Google Gemini, Cohere, local CLI."""
import re
import shlex
import shutil
import subprocess
import tempfile

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


# What a signed-in CLI prints when its subscription or the service is not available right now.
_CLI_OUTAGE = re.compile(
    r"usage limit|limit reached|hit your .*limit|rate.?limit|too many requests|quota|overloaded|"
    r"over capacity|try again|credit balance|insufficient|resets? (?:at|in)|\b(?:429|500|502|503|504|529)\b|"
    r"unavailable|temporar|connection|network|timed? ?out|stream (?:disconnected|error)|"
    r"memory allocation|out of memory|cannot allocate|not logged in|please run /login|"
    r"authentication|unauthorized|log ?in", re.I)


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
    Because it executes a program, it only works when the system
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
        if not shutil.which(cmd[0]):
            raise ProviderError("command not found: %s" % cmd[0])
        prompt = system + "\n\n" + user
        as_argument = any("{prompt}" in arg for arg in cmd)
        if as_argument:
            cmd = [arg.replace("{prompt}", prompt) for arg in cmd]
        # Odoo workers cap the address space (limit_memory_hard); Go based tools such as agy
        # reserve far more virtual memory and die with "out of memory", so lift the soft limit.
        cmd = ["sh", "-c", LIFT_MEMORY_LIMIT, "sh"] + cmd
        try:
            with tempfile.TemporaryDirectory() as cwd:
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
