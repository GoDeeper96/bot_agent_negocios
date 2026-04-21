#!/usr/bin/env python3
"""
One-time script — authenticates Microsoft Graph API for email sending.
Uses authorization code + PKCE flow (works even with enterprise CA policies).

Pre-requisites in Azure portal:
  1. App registered with Mail.Send delegated permission
  2. Redirect URI added: http://localhost:8080/callback  (Mobile and desktop)
  3. Allow public client flows: Yes

Usage:
    python scripts/get_graph_token.py
"""
import base64
import hashlib
import http.server
import secrets
import sys
import time
import urllib.parse
import webbrowser

import boto3
import requests

CLIENT_ID   = "39ee32f7-f9d3-46af-831e-4a614f2aa743"
TENANT      = "common"
SCOPES      = "Mail.Send offline_access"
REDIRECT    = "http://localhost:8080/callback"
SSM_REGION  = "us-west-2"
SSM_PROFILE = "eyatech"

AUTH_URL    = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL   = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


def _pkce():
    verifier   = secrets.token_urlsafe(64)
    challenge  = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _build_auth_url(verifier, challenge, state):
    params = {
        "client_id":             CLIENT_ID,
        "response_type":         "code",
        "redirect_uri":          REDIRECT,
        "scope":                 SCOPES,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "prompt":                "select_account",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


_callback_result: dict = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _callback_result["code"] = params["code"][0]
            _callback_result["state"] = params.get("state", [""])[0]
            body = b"<h2>Autenticado correctamente. Puedes cerrar esta ventana.</h2>"
        else:
            _callback_result["error"] = params.get("error", ["unknown"])[0]
            body = b"<h2>Error de autenticacion. Revisa la consola.</h2>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress server logs


def main():
    stage = input("Stage [dev/prod]: ").strip() or "dev"
    ssm_prefix = f"/bot-agent/{stage}"

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(verifier, challenge, state)

    print("\nAbriendo el navegador para autenticacion...")
    print("Inicia sesion con: negocios.lichan@outlook.com\n")
    time.sleep(1)
    webbrowser.open(auth_url)

    # Start local server — waits for one callback request
    server = http.server.HTTPServer(("localhost", 8080), _Handler)
    server.handle_request()

    if "error" in _callback_result:
        print(f"\nError de autenticacion: {_callback_result['error']}")
        sys.exit(1)

    code = _callback_result.get("code")
    if not code:
        print("\nNo se recibio codigo de autorizacion.")
        sys.exit(1)

    print("Codigo recibido. Obteniendo tokens...")

    resp = requests.post(TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT,
        "code_verifier": verifier,
        "scope":         SCOPES,
    }, timeout=15)
    resp.raise_for_status()
    tokens = resp.json()

    if "refresh_token" not in tokens:
        print(f"\nNo se obtuvo refresh_token: {tokens}")
        sys.exit(1)

    print("Guardando en SSM...")

    session = boto3.Session(profile_name=SSM_PROFILE, region_name=SSM_REGION)
    ssm     = session.client("ssm")

    ssm.put_parameter(
        Name=f"{ssm_prefix}/graph/client_id",
        Value=CLIENT_ID,
        Type="String",
        Overwrite=True,
    )
    ssm.put_parameter(
        Name=f"{ssm_prefix}/graph/refresh_token",
        Value=tokens["refresh_token"],
        Type="SecureString",
        Overwrite=True,
    )

    print(f"\n  Guardado: {ssm_prefix}/graph/client_id")
    print(f"  Guardado: {ssm_prefix}/graph/refresh_token  (cifrado)")
    print("\nListo. El bot puede enviar emails desde negocios.lichan@outlook.com")


if __name__ == "__main__":
    main()
