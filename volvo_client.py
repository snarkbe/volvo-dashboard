"""Volvo Connected Vehicle + Energy API client.

Exposes GET /status returning aggregated JSON with battery %, charging
state, lock state, remaining electric range, and fetch timestamp.

Auth model (Volvo does NOT accept username/password):
  1. Register an app at https://developer.volvocars.com -> get VCC_API_KEY,
     VOLVO_CLIENT_ID, VOLVO_CLIENT_SECRET, and a redirect URI.
  2. Run `python volvo_client.py authorize` once in a browser to obtain
     a refresh_token. It is written to TOKEN_FILE.
  3. The service uses that refresh_token to mint access tokens on demand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests
from flask import Flask, jsonify

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("volvo")

VIN = os.environ["VOLVO_VIN"]
CLIENT_ID = os.environ["VOLVO_CLIENT_ID"]
CLIENT_SECRET = os.environ["VOLVO_CLIENT_SECRET"]
VCC_API_KEY = os.environ["VCC_API_KEY"]
REDIRECT_URI = os.environ.get("VOLVO_REDIRECT_URI", "http://localhost:4000/callback")
TOKEN_FILE = Path(os.environ.get("VOLVO_TOKEN_FILE", "./data/token.json"))

# Volvo ID moved to Keycloak. New apps get a `vid-dc-...` client id and must
# use the new realm; legacy `dc-...` clients only work on the old server, which
# is retired after 2027-01-31.
_KEYCLOAK = CLIENT_ID.startswith("vid-")
if _KEYCLOAK:
    _OIDC = "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect"
    AUTH_URL = f"{_OIDC}/auth"
    TOKEN_URL = f"{_OIDC}/token"
else:
    AUTH_URL = "https://volvoid.eu.volvocars.com/as/authorization.oauth2"
    TOKEN_URL = "https://volvoid.eu.volvocars.com/as/token.oauth2"
API_BASE = "https://api.volvocars.com"

SCOPES = [
    "openid",
    "conve:vehicle_relation",
    "conve:doors_status",
    "conve:lock_status",
    "conve:odometer_status",
    "conve:battery_charge_level",
    "conve:fuel_status",
    "conve:tyre_status",
    "energy:state:read",
]
if _KEYCLOAK and os.environ.get("VOLVO_OFFLINE_ACCESS", "0") == "1":
    # Opt-in: the developer portal currently doesn't offer this scope, and
    # requesting it unassigned fails authorize with invalid_scope. Without offline_access Keycloak ties the refresh token to the browser
    # SSO session (hours/days) and the headless service loses access.
    SCOPES.append("offline_access")

_token_lock = Lock()
_cached_access_token: dict | None = None


def _basic_auth_header() -> str:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _load_refresh_token() -> str:
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"No token at {TOKEN_FILE}. Run `python volvo_client.py authorize` first."
        )
    return json.loads(TOKEN_FILE.read_text())["refresh_token"]


def _save_refresh_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"refresh_token": token}))


def _get_access_token() -> str:
    global _cached_access_token
    with _token_lock:
        now = time.time()
        if _cached_access_token and _cached_access_token["expires_at"] - 60 > now:
            return _cached_access_token["access_token"]

        log.debug("Refreshing access token at %s", TOKEN_URL)
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": _load_refresh_token(),
            },
            timeout=15,
        )
        if not resp.ok:
            log.error(
                "Token refresh failed: HTTP %s %s", resp.status_code, resp.text[:500]
            )
            if "invalid_grant" in resp.text:
                log.error(
                    "Refresh token rejected (expired, already used, or issued for "
                    "another client). Re-run `python volvo_client.py authorize`."
                )
        resp.raise_for_status()
        body = resp.json()
        rotated = "refresh_token" in body
        if rotated:
            _save_refresh_token(body["refresh_token"])
        expires_in = int(body.get("expires_in", 1800))
        log.info(
            "Access token refreshed (expires in %ss, refresh token %s, "
            "refresh valid %ss)",
            expires_in,
            "rotated + saved" if rotated else "unchanged",
            body.get("refresh_expires_in", "?"),
        )
        _cached_access_token = {
            "access_token": body["access_token"],
            "expires_at": now + expires_in,
        }
        return _cached_access_token["access_token"]


def _api_get(path: str, accept: str = "application/json") -> dict:
    started = time.monotonic()
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_get_access_token()}",
            "vcc-api-key": VCC_API_KEY,
            "accept": accept,
        },
        timeout=15,
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    if resp.ok:
        log.debug("GET %s -> %s (%.0f ms)", path, resp.status_code, elapsed_ms)
    else:
        log.warning(
            "GET %s -> %s (%.0f ms): %s",
            path, resp.status_code, elapsed_ms, resp.text[:500],
        )
    resp.raise_for_status()
    return resp.json()


def _dig(obj: dict, *path: Any, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def fetch_status() -> dict:
    with ThreadPoolExecutor(max_workers=4) as pool:
        doors_f = pool.submit(_api_get, f"/connected-vehicle/v2/vehicles/{VIN}/doors")
        energy_f = pool.submit(_api_get, f"/energy/v2/vehicles/{VIN}/state")
        odo_f = pool.submit(_api_get, f"/connected-vehicle/v2/vehicles/{VIN}/odometer")
        tyres_f = pool.submit(_api_get, f"/connected-vehicle/v2/vehicles/{VIN}/tyres")
        doors, energy, odo, tyres = doors_f.result(), energy_f.result(), odo_f.result(), tyres_f.result()

    locked_raw = _dig(doors, "data", "centralLock", "value")
    charging_status = _dig(energy, "chargingStatus", "value")
    odometer_raw = _dig(odo, "data", "odometer", "value")

    tyre_pressures = None
    if tyres and "data" in tyres:
        tyre_data = tyres.get("data", {})
        tyre_pressures = {
            "front_left": _dig(tyre_data, "frontLeft", "value"),
            "front_right": _dig(tyre_data, "frontRight", "value"),
            "rear_left": _dig(tyre_data, "rearLeft", "value"),
            "rear_right": _dig(tyre_data, "rearRight", "value"),
        }

    return {
        "battery_pct": _dig(energy, "batteryChargeLevel", "value"),
        "charging_status": charging_status,
        "range_km": _dig(energy, "electricRange", "value"),
        "charger_connection_status": _dig(energy, "chargerConnectionStatus", "value"),
        "charging_type": _dig(energy, "chargingType", "value"),
        "charger_power_status": _dig(energy, "chargerPowerStatus", "value"),
        "estimated_charging_time_minutes": _dig(energy, "estimatedChargingTimeToTargetBatteryChargeLevel", "value"),
        "target_battery_pct": _dig(energy, "targetBatteryChargeLevel", "value"),
        "charging_current_limit_amp": _dig(energy, "chargingCurrentLimit", "value"),
        "charging_power_watts": _dig(energy, "chargingPower", "value"),
        "odometer_km": float(odometer_raw) if odometer_raw is not None else None,
        "locked": locked_raw,
        "tyre_pressures": tyre_pressures,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _log_startup() -> None:
    version_file = Path(__file__).with_name("VERSION")
    version = version_file.read_text().strip() if version_file.exists() else "dev"
    log.info("volvo-dashboard %s starting (pid %s)", version, os.getpid())
    log.info("VIN: %s…%s", VIN[:3], VIN[-4:])
    log.info(
        "Auth: %s client %s… -> %s",
        "Volvo ID (Keycloak)" if _KEYCLOAK else "legacy Volvo ID (retired after 2027-01-31)",
        CLIENT_ID[:8], TOKEN_URL,
    )
    log.info("Scopes: %s", " ".join(SCOPES))
    if TOKEN_FILE.exists():
        mtime = datetime.fromtimestamp(TOKEN_FILE.stat().st_mtime, timezone.utc)
        log.info("Token file: %s (last written %s)", TOKEN_FILE, mtime.isoformat(timespec="seconds"))
    else:
        log.warning(
            "Token file %s not found - run `python volvo_client.py authorize` "
            "and copy it here", TOKEN_FILE,
        )
    if not _KEYCLOAK:
        log.warning("Legacy dc- client id in use; migrate to a vid- app before 2027-01-31")


app = Flask(__name__)
if __name__ != "__main__" or len(sys.argv) == 1:
    _log_startup()


@app.get("/status")
def status():
    try:
        started = time.monotonic()
        data = fetch_status()
        log.info(
            "/status ok in %.0f ms: battery=%s%% range=%skm charging=%s locked=%s",
            (time.monotonic() - started) * 1000,
            data["battery_pct"], data["range_km"],
            data["charging_status"], data["locked"],
        )
        return jsonify(data)
    except requests.HTTPError as e:
        log.error("/status failed: Volvo API HTTP %s for %s",
                  e.response.status_code, e.response.url)
        return (
            jsonify({
                "error": "volvo_api",
                "status": e.response.status_code,
                "url": e.response.url,
                "body": e.response.text,
            }),
            502,
        )
    except KeyError as e:
        return jsonify({"error": "missing_env", "var": str(e)}), 500
    except Exception as e:
        log.exception("/status failed")
        return jsonify({"error": type(e).__name__, "detail": str(e)}), 500


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _authorize_flow() -> None:
    """One-time interactive OAuth2 PKCE flow to obtain a refresh_token."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"Opening browser. If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)

    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.urlparse(self.path).query
            captured.update(urllib.parse.parse_qs(qs))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"You can close this tab.")

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            del format, args

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    if not parsed.hostname:
        raise RuntimeError(f"VOLVO_REDIRECT_URI missing host: {REDIRECT_URI!r}")
    httpd = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    httpd.handle_request()

    if captured.get("state", [None])[0] != state:
        raise RuntimeError("OAuth state mismatch")

    if "error" in captured:
        desc = captured.get("error_description", [""])[0]
        raise RuntimeError(f"OAuth error: {captured['error'][0]} {desc}".rstrip())

    if "code" not in captured:
        raise RuntimeError(f"No authorization code received. Callback parameters: {captured}")

    code = captured["code"][0]

    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    _save_refresh_token(body["refresh_token"])
    print(f"Refresh token saved to {TOKEN_FILE}")
    print(
        f"access expires_in={body.get('expires_in')}s, "
        f"refresh_expires_in={body.get('refresh_expires_in')}s (0 = no expiry / offline)"
    )


def _print_scopes() -> None:
    tok = _get_access_token()
    payload_b64 = tok.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    print(json.dumps(
        {"scope": claims.get("scope"), "aud": claims.get("aud"), "sub": claims.get("sub")},
        indent=2,
    ))


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["authorize"]:
            _authorize_flow()
        case ["token"]:
            print(_get_access_token())
        case ["vehicles"]:
            print(json.dumps(_api_get("/connected-vehicle/v2/vehicles"), indent=2))
        case ["raw", path]:
            print(json.dumps(_api_get(path), indent=2))
        case ["scopes"]:
            _print_scopes()
        case _:
            app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
