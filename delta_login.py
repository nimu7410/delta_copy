"""Login / authenticated session for Delta Exchange India.

One venue: **live**. The demo venue was removed - its instrument list carried
only four of the ten symbols, its market-data socket was unreachable from this
network, and its traded prices drifted as far as 15% from the real market, so
nothing measured on it meant anything. Paper trading is now a *mode* of this
bot against real market data (`TRADE_MODE=TEST`), not a separate exchange.

Usage:
    from delta_login import login
    delta = login()                       # reads credentials, verifies them
    print(delta.get("/v2/profile"))
    print(delta.get("/v2/wallet/balances"))

Credentials are resolved in this order, first hit wins:
    1. explicit api_key/api_secret arguments
    2. env vars DELTA_API_KEY / DELTA_API_SECRET
    3. AWS Secrets Manager (secret `Delta_Auth` in ap-south-1)
    4. a .env file in this directory
    5. a `key*.txt` file in this directory ("key: ...", "secret: ..." lines)

AWS sits above the local files so a deployment reads the managed copy, and
below the environment so an operator can still override it for one run without
touching the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger("login")

BASE_URL = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")
WS_URL = os.environ.get("DELTA_WS_URL", "wss://socket.india.delta.exchange")
HERE = Path(__file__).resolve().parent

#: Where the managed credentials live. Holds `Key` and `Secret`.
AWS_SECRET_NAME = os.environ.get("DELTA_SECRET_NAME", "Delta_Auth")
AWS_SECRET_REGION = os.environ.get("DELTA_SECRET_REGION") or os.environ.get(
    "AWS_REGION", "ap-south-1"
)
#: Spellings accepted inside the secret, so renaming a field there is not a
#: silent outage.
_KEY_FIELDS = ("Key", "DELTA_API_KEY", "api_key", "key")
_SECRET_FIELDS = ("Secret", "DELTA_API_SECRET", "api_secret", "secret")

TIMEOUT = (10, 30)          # (connect, read) seconds
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}


class DeltaError(Exception):
    """Any Delta API / auth failure."""

    def __init__(self, message, status=None, code=None, payload=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload


class DeltaAuthError(DeltaError):
    """Credentials are missing, malformed, rejected, or IP-blocked."""


def _clean(value):
    """Strip whitespace/quotes that sneak in from copy-paste and .env files."""
    return (value or "").strip().strip("'\"").strip()


def _from_env():
    return _clean(os.environ.get("DELTA_API_KEY")), _clean(os.environ.get("DELTA_API_SECRET"))


def _from_aws():
    """Read the managed credentials, or return blanks if unavailable.

    Never raises: a missing role, an unreachable region or a denied secret
    must fall through to the local files rather than stop the bot. The reason
    is logged once so a silent fallback is still a visible one.
    """
    try:
        import boto3
    except ImportError:
        log.debug("boto3 not installed; skipping AWS Secrets Manager")
        return "", ""
    try:
        client = boto3.client("secretsmanager", region_name=AWS_SECRET_REGION)
        payload = client.get_secret_value(
            SecretId=AWS_SECRET_NAME, VersionStage="AWSCURRENT"
        )
        values = json.loads(payload["SecretString"])
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.info(
            "AWS Secrets Manager (%s @ %s) unavailable: %s. Falling back to "
            "local credentials.",
            AWS_SECRET_NAME, AWS_SECRET_REGION, type(exc).__name__,
        )
        return "", ""

    key = next((_clean(values[f]) for f in _KEY_FIELDS if values.get(f)), "")
    secret = next((_clean(values[f]) for f in _SECRET_FIELDS if values.get(f)), "")
    if key and secret:
        log.info("Credentials resolved from AWS Secrets Manager (%s @ %s)",
                 AWS_SECRET_NAME, AWS_SECRET_REGION)
    else:
        log.warning(
            "Secret %s carries %s; expected a key field (%s) and a secret field (%s).",
            AWS_SECRET_NAME, sorted(values), "/".join(_KEY_FIELDS), "/".join(_SECRET_FIELDS),
        )
    return key, secret


def _from_files():
    """Parse key/secret out of .env or key*.txt, whichever exists."""
    for path in [HERE / ".env", *sorted(HERE.glob("key*.txt"))]:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        found = {}
        for line in text.splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_]+)\s*[:=]\s*(.+)", line)
            if not m:
                continue
            name, value = m.group(1).lower(), _clean(m.group(2))
            if "secret" in name:
                found.setdefault("secret", value)
            elif "key" in name:
                found.setdefault("key", value)
        if found.get("key") and found.get("secret"):
            log.info("Credentials resolved from %s", path.name)
            return found["key"], found["secret"]
    return "", ""


def load_credentials(api_key=None, api_secret=None):
    key, secret = _clean(api_key), _clean(api_secret)
    for source in (_from_env, _from_aws, _from_files):
        if key and secret:
            break
        found_key, found_secret = source()
        key = key or found_key
        secret = secret or found_secret
    if not key or not secret:
        raise DeltaAuthError(
            f"No API credentials found. Set DELTA_API_KEY and DELTA_API_SECRET, "
            f"store them in AWS secret {AWS_SECRET_NAME} ({AWS_SECRET_REGION}), "
            f"or put them in {HERE / '.env'}"
        )
    return key, secret


class DeltaClient:
    """Signed HTTP session against Delta Exchange India.

    Handles clock skew, transient network errors and expired signatures.
    """

    def __init__(self, api_key, api_secret, base_url=BASE_URL, timeout=TIMEOUT):
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._skew = 0.0          # server_time - local_time, seconds
        self.profile = None
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "python-delta-login",
        })

    # -- signing ---------------------------------------------------------
    def _timestamp(self):
        return str(int(time.time() + self._skew))

    def _sign(self, method, path, query, body, timestamp):
        message = method + timestamp + path + query + body
        return hmac.new(self._secret, message.encode(), hashlib.sha256).hexdigest()

    def sync_time(self):
        """Align our clock with the exchange; signatures die outside a 5s window."""
        data = self._request("GET", "/v2/settings", signed=False, _no_resync=True)
        server_us = data.get("server_time")
        if server_us:
            self._skew = server_us / 1_000_000 - time.time()
        return self._skew

    # -- transport -------------------------------------------------------
    def _request(self, method, path, params=None, payload=None, signed=True, _no_resync=False):
        method = method.upper()
        query = ""
        if params:
            query = "?" + urlencode({k: v for k, v in params.items() if v is not None})
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        url = self.base_url + path + query

        last_error = None
        for attempt in range(MAX_RETRIES):
            headers = {}
            if signed:
                ts = self._timestamp()
                headers = {
                    "api-key": self.api_key,
                    "timestamp": ts,
                    "signature": self._sign(method, path, query, body, ts),
                }
            try:
                response = self.session.request(
                    method, url, headers=headers,
                    data=body or None, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = DeltaError(f"{method} {path} failed: {exc}")
                time.sleep(2 ** attempt)
                continue

            try:
                data = response.json()
            except ValueError:
                data = None

            if response.ok and isinstance(data, dict) and data.get("success"):
                return data.get("result", data)

            code, context = _error_details(data)

            # Clock drifted or signature stale -> resync once and retry.
            if code in ("expired_signature", "invalid_api_key_timestamp") and not _no_resync:
                self.sync_time()
                _no_resync = True
                continue

            if response.status_code in (401, 403) or code in _AUTH_CODES:
                raise DeltaAuthError(
                    _auth_hint(code, context), response.status_code, code, data
                )

            if response.status_code in RETRY_STATUS:
                last_error = DeltaError(
                    f"{method} {path} -> HTTP {response.status_code}",
                    response.status_code, code, data,
                )
                time.sleep(2 ** attempt)
                continue

            raise DeltaError(
                f"{method} {path} -> HTTP {response.status_code}: {code or response.text[:200]}",
                response.status_code, code, data,
            )

        raise last_error or DeltaError(f"{method} {path} failed after {MAX_RETRIES} attempts")

    # -- public ----------------------------------------------------------
    def get(self, path, params=None, signed=True):
        return self._request("GET", path, params=params, signed=signed)

    def post(self, path, payload=None, params=None):
        return self._request("POST", path, params=params, payload=payload or {})

    def delete(self, path, payload=None, params=None):
        return self._request("DELETE", path, params=params, payload=payload)

    def verify(self):
        """The actual 'login': prove the key works and cache the profile."""
        self.sync_time()
        self.profile = self.get("/v2/profile")
        return self.profile

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_AUTH_CODES = {
    "invalid_api_key", "unauthorized", "invalid_signature", "expired_signature",
    "ip_blocked_for_api_key", "UnauthorizedApiAccess", "invalid_api_key_timestamp",
    "api_key_expired", "insufficient_permissions",
}

_HINTS = {
    "invalid_api_key": (
        "API key is not recognised. This bot trades the LIVE India venue "
        f"({BASE_URL}); a key issued for the demo venue will not work here."
    ),
    "invalid_signature": "Signature rejected. The API secret is wrong or was truncated when copied.",
    "expired_signature": "Request timestamp outside the exchange's window even after clock sync — check the system clock.",
    "ip_blocked_for_api_key": "This machine's IP is not whitelisted for the key. Add it in Delta > API Keys.",
    "api_key_expired": "API key has expired; generate a new one.",
    "insufficient_permissions": "API key lacks the permission this endpoint needs.",
}


def _error_details(data):
    if not isinstance(data, dict):
        return None, None
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("context")
    if isinstance(error, str):
        return error, None
    return data.get("message"), None


def _auth_hint(code, context):
    hint = _HINTS.get(code, f"Authentication failed ({code or 'unknown error'}).")
    if code == "ip_blocked_for_api_key" and isinstance(context, dict):
        ip = context.get("client_ip") or context.get("ip")
        if ip:
            hint += f" Current IP: {ip}."
    return hint


def login(api_key=None, api_secret=None, base_url=None, verify=True):
    """Return a ready-to-use authenticated DeltaClient."""
    key, secret = load_credentials(api_key, api_secret)
    client = DeltaClient(key, secret, base_url=base_url or BASE_URL)
    if verify:
        client.verify()
    return client


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    # self-check: signing is deterministic and matches the documented recipe
    c = DeltaClient("k", "s")
    expected = hmac.new(b"s", b"GET1700000000/v2/profile", hashlib.sha256).hexdigest()
    assert c._sign("GET", "/v2/profile", "", "", "1700000000") == expected
    assert _error_details({"error": {"code": "invalid_api_key"}})[0] == "invalid_api_key"
    assert _clean(" 'abc' ") == "abc"

    try:
        delta = login()
    except DeltaError as exc:
        raise SystemExit(f"Login failed: {exc}")
    p = delta.profile
    print(f"Logged in: {p.get('email')} (id {p.get('id')})")
    print("Balances:", [
        f"{b['asset_symbol']}={b['balance']}"
        for b in delta.get("/v2/wallet/balances") if float(b["balance"]) > 0
    ] or "none")
    delta.close()
