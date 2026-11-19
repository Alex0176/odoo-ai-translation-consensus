# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Chat models: OpenAI-compatible APIs, Anthropic, Google Gemini, Cohere, local CLI."""
import base64
import binascii
import contextlib
import fcntl
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time

from urllib.parse import urlparse

from . import documents
from .base import ChatProvider
from .http import ProviderError, ProviderUnavailable, call

# How many command line tools may run at the same time ON THIS HOST, over all Odoo processes.
# The engine asks many providers in parallel threads, in every worker and in the cron process; a
# command line assistant is a full program of its own (several hundred megabytes each, and the
# memory limit of the worker is lifted for it). Without a limit a queue of translations starts a
# dozen of them at once and the kernel kills the Odoo service for lack of memory.
COMMAND_SLOTS = max(1, int(os.environ.get("AI_TRANSLATION_COMMAND_SLOTS") or 2))
SLOT_DIR = os.path.join(tempfile.gettempdir(), "ai_translation_consensus_slots")


def _run_group(cmd, text_input, timeout, cwd):
    """``subprocess.run`` in a process group of its own; a timeout ends the whole group."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if text_input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
                            start_new_session=True)
    try:
        out, err = proc.communicate(text_input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid, signal.SIGKILL)
            proc.communicate()
        raise
    finally:
        _kill_group(proc.pid, signal.SIGKILL)         # helpers that outlived the main process
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


@contextlib.contextmanager
def command_slot(slots=None, directory=None):
    """Wait for one of the ``slots`` places and hold it while a command runs.

    The places are lock files, so the limit holds across threads and across processes. Waiting is
    not an error and does not count against the time limit of the command: the call simply starts
    later.
    """
    slots = slots or COMMAND_SLOTS
    directory = directory or SLOT_DIR
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handles = [open(os.path.join(directory, "slot%d.lock" % number), "a") for number in range(slots)]
    held = None
    try:
        while held is None:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                held = handle
                break
            else:
                time.sleep(0.5)
        yield
    finally:
        if held is not None:
            fcntl.flock(held, fcntl.LOCK_UN)
        for handle in handles:
            handle.close()



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
    #: For tools without an image option (Claude Code, Gemini CLI): all paths inside ONE argument,
    #: separated by spaces - typically in an instruction such as "read these files first: {image_paths}".
    IMAGE_PATHS = "{image_paths}"
    #: ``[[ ... ]]`` inside an argument is only kept for document requests (brackets removed);
    #: for a plain text request the whole part is left out.
    OPTIONAL = re.compile(r"\[\[(.*?)\]\]", re.S)

    @classmethod
    def documents_capable(cls, cfg):
        command = cfg.get("command") or ""
        return cls.IMAGES in command or cls.IMAGE_PATHS in command

    def _complete(self, system, user):
        return self._run(system, user, None)

    def _complete_document(self, system, user, files):
        return self._run(system, user, files)

    def _expand_images(self, cmd, paths):
        """``cmd`` with the placeholder replaced by ``paths`` (own temporary files); no shell is involved."""
        out = []
        for arg in cmd:
            arg = self.OPTIONAL.sub(lambda m: m.group(1), arg)
            if self.IMAGE_PATHS in arg:
                arg = arg.replace(self.IMAGE_PATHS, " ".join(paths))
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
            arg = self.OPTIONAL.sub("", arg).replace(self.IMAGE_PATHS, "")
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
        if files and not any(self.IMAGES in arg or self.IMAGE_PATHS in arg for arg in cmd):
            raise ProviderError("the command has no %s or %s placeholder, so it cannot read documents"
                                % (self.IMAGES, self.IMAGE_PATHS))
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
                # CLI tools start helpers of their own (the gemini wrapper spawns a second node
                # process). Killing only the direct child on a timeout left those running for
                # minutes with more than 1 GB each. So: own process group, killed as a whole; and
                # GNU timeout as a watchdog that still ends the group when this worker itself dies.
                if shutil.which("timeout"):
                    cmd = ["timeout", "--kill-after=5", str(int(self.timeout) + 15)] + cmd
                with command_slot():
                    proc = _run_group(cmd, None if as_argument else prompt, self.timeout, cwd)
        except FileNotFoundError:
            raise ProviderError("command not found: %s" % cmd[0])
        except subprocess.TimeoutExpired:
            raise ProviderUnavailable("command timed out after %s s" % self.timeout)
        if proc.returncode != 0:
            raise _cli_failure(proc.returncode, proc.stdout, proc.stderr)
        return proc.stdout
