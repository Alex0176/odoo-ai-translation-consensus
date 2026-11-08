# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""How much of the plan of a signed-in command line tool is used up.

Odoo independent. ``read(command)`` returns ``{"windows": [...], "free_resets": n}`` or ``None``
for a tool without a usage report. A window is ``{"name", "window", "minutes", "used", "resets_at"}``:
``used`` in percent, ``resets_at`` as UTC ``YYYY-MM-DD HH:MM:SS``.
"""
import contextlib
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from .chat import LIFT_MEMORY_LIMIT
from .http import ProviderError

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_WORD_MINUTES = {"weekly": 7 * 24 * 60, "daily": 24 * 60, "hourly": 60}


def _executable(command):
    parts = shlex.split(command or "")
    return parts[0] if parts else ""


def supported(command):
    """True when the tool of this command line can report its usage."""
    return os.path.basename(_executable(command)) in _READERS


def _utc(value):
    """UTC string of an epoch (seconds) or an ISO 8601 text; None when it is neither."""
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@contextlib.contextmanager
def _process(argv, stdin=None, stderr=subprocess.PIPE):
    """The tool as a child process in an empty directory; killed when the block ends."""
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.Popen(["sh", "-c", LIFT_MEMORY_LIMIT, "sh"] + argv, stdin=stdin,
                                stdout=subprocess.PIPE, stderr=stderr, cwd=cwd)
        try:
            yield proc
        finally:
            proc.kill()
            proc.communicate()


# ---------------------------------------------------------------- Antigravity: ``agy -p /usage``
_REMAINING = re.compile(r"^(?P<what>.*?)\s*(?P<kind>Remaining|Used)\s*$", re.I)


def parse_agy(text):
    """Tab separated lines: ``Gemini Models<TAB>Weekly Limit Remaining<TAB>51%<TAB>2026-11-13T23:44:45Z``."""
    windows = []
    for line in (text or "").splitlines():
        cols = [col.strip() for col in line.split("\t")]
        if len(cols) < 3:
            continue
        match = _REMAINING.match(cols[1])
        percent = re.match(r"^(\d+(?:[.,]\d+)?)\s*%$", cols[2])
        if not match or not percent:
            continue
        share = float(percent.group(1).replace(",", "."))
        used = 100.0 - share if match.group("kind").lower() == "remaining" else share
        what = match.group("what")
        first = what.split()[0].lower() if what.split() else ""
        windows.append({"name": cols[0], "window": what, "minutes": _WORD_MINUTES.get(first),
                        "used": max(0.0, min(100.0, used)),
                        "resets_at": _utc(cols[3]) if len(cols) > 3 else None})
    if not windows:
        raise ProviderError("no usage figures in the answer: %s" % re.sub(r"\s+", " ", text or "")[:200])
    return {"windows": windows, "free_resets": 0}


def _read_agy(executable, timeout):
    with _process([executable, "-p", "/usage"]) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError("the usage report timed out after %s s" % timeout)
        if proc.returncode != 0:
            raise ProviderError("exit code %s: %s" % (proc.returncode, re.sub(
                r"\s+", " ", (err or b"").decode("utf-8", "replace"))[-250:]))
        return parse_agy(out.decode("utf-8", "replace"))


# ---------------------------------------------------------------- Codex: app server ``account/rateLimits/read``
def parse_codex(result):
    """``result`` of the ``account/rateLimits/read`` call of ``codex app-server``."""
    result = result or {}
    by_id = result.get("rateLimitsByLimitId") or {}
    snapshots = list(by_id.values()) or ([result["rateLimits"]] if result.get("rateLimits") else [])
    windows = []
    for snap in snapshots:
        for slot in ("primary", "secondary"):
            win = snap.get(slot)
            if not win or win.get("usedPercent") is None:
                continue
            windows.append({"name": snap.get("limitName") or "", "window": "", "minutes": win.get("windowDurationMins"),
                            "used": max(0.0, min(100.0, float(win["usedPercent"]))),
                            "resets_at": _utc(win.get("resetsAt"))})
    if not windows:
        raise ProviderError("no usage figures in the answer: %s" % json.dumps(result)[:200])
    credits = (result.get("rateLimitResetCredits") or {}).get("credits") or []
    free = sum(1 for c in credits if c.get("status") == "available")
    return {"windows": windows, "free_resets": free}


def _read_codex(executable, timeout):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "odoo-ai-translation-usage", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"},
    ]
    with _process([executable, "app-server"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        proc.stdin.write(("\n".join(json.dumps(r) for r in requests) + "\n").encode())
        proc.stdin.flush()
        fd, buffer, deadline = proc.stdout.fileno(), b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _w, _x = select.select([fd], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("id") == 2:
                    if message.get("error"):
                        raise ProviderError("usage request refused: %s" % json.dumps(message["error"])[:200])
                    return parse_codex(message.get("result"))
        raise ProviderError("no answer to the usage request within %s s" % timeout)


_READERS = {"agy": _read_agy, "codex": _read_codex}


def read(command, timeout=45):
    executable = _executable(command)
    reader = _READERS.get(os.path.basename(executable))
    if not reader:
        return None
    if not shutil.which(executable):
        raise ProviderError("command not found: %s" % executable)
    return reader(executable, timeout)
