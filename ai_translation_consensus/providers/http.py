# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import re
import time

import requests


class ProviderError(Exception):
    """A provider failed; the message is safe to store and show (no secrets)."""


_sleep = time.sleep   # replaced in tests
_clock = time.monotonic
_last_call = {}


def throttle(key, seconds):
    """Keep ``seconds`` between two calls with the same key (free tiers allow few calls)."""
    seconds = float(seconds or 0)
    if seconds > 0:
        wait = _last_call.get(key, -1e9) + seconds - _clock()
        if wait > 0:
            _sleep(wait)
    _last_call[key] = _clock()


def _retry_after(resp):
    """Seconds the server asks us to wait, or None. Handles the header and 'Retry after 39s'."""
    try:
        return min(float(resp.headers.get("Retry-After", "")), 60)
    except ValueError:
        pass
    if resp.status_code in (402, 429):
        match = re.search(r"retry after (\d+)", resp.text or "", re.I)
        if match:
            return min(float(match.group(1)) + 1, 60)
    return None


def _clean(text, secrets, limit=300):
    text = (text or "").replace("\n", " ")[:limit]
    for secret in secrets or ():
        if secret:
            text = text.replace(secret, "***")
    return text


def call(method, url, *, headers=None, json_body=None, params=None, timeout=120,
         retries=2, secrets=()):
    """HTTP request returning parsed JSON; retries on 429/5xx and network errors."""
    last = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, json=json_body,
                                    params=params, timeout=timeout)
        except requests.RequestException as exc:
            last = ProviderError("%s: %s" % (type(exc).__name__, _clean(str(exc), secrets, 150)))
            if attempt < retries:
                _sleep(1 + 2 * attempt)
            continue
        wait = _retry_after(resp)
        retryable = resp.status_code in (429, 500, 502, 503, 504) or (
            resp.status_code == 402 and wait is not None)
        if retryable and attempt < retries:
            last = ProviderError("HTTP %s" % resp.status_code)
            _sleep(wait if wait is not None else 1 + 3 * attempt)
            continue
        if resp.status_code >= 400:
            raise ProviderError("HTTP %s: %s" % (resp.status_code, _clean(resp.text, secrets)))
        try:
            return resp.json()
        except ValueError:
            raise ProviderError("server did not return JSON: %s" % _clean(resp.text, secrets, 120))
    raise last or ProviderError("request failed")
