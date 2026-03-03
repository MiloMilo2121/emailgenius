from __future__ import annotations

import getpass
import os
from pathlib import Path

from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import app_home

DEFAULT_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]


def oauth_token_path() -> Path:
    override = (os.getenv("EMAILGENIUS_GOOGLE_OAUTH_TOKEN_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return app_home() / "google-oauth-token.json"


def oauth_client_id() -> str | None:
    value = (os.getenv("EMAILGENIUS_GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    return value or None


def oauth_client_secret() -> str | None:
    value = (
        os.getenv("EMAILGENIUS_GOOGLE_OAUTH_CLIENT_SECRET")
        or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        or ""
    ).strip()
    return value or None


def oauth_local_port() -> int:
    raw = (os.getenv("EMAILGENIUS_GOOGLE_OAUTH_LOCAL_PORT") or "").strip()
    if not raw:
        return 53877
    try:
        port = int(raw)
    except ValueError:
        return 53877
    if port <= 0 or port > 65535:
        return 53877
    return port


def _load_oauth_credentials(path: Path, scopes: list[str]) -> UserCredentials | None:
    if not path.exists():
        return None
    try:
        return UserCredentials.from_authorized_user_file(str(path), scopes=scopes)
    except Exception:
        return None


def _save_oauth_credentials(creds: UserCredentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


def resolve_google_credentials(
    *,
    service_account_json: str | None,
    interactive: bool,
    scopes: list[str] | None = None,
) -> Credentials:
    resolved_scopes = list(scopes or DEFAULT_GOOGLE_SCOPES)

    if service_account_json:
        return ServiceAccountCredentials.from_service_account_file(service_account_json, scopes=resolved_scopes)

    token_path = oauth_token_path()
    creds = _load_oauth_credentials(token_path, resolved_scopes)

    if creds is None:
        client_id = oauth_client_id()
        client_secret = oauth_client_secret()
        if not (client_id and client_secret):
            if not interactive:
                raise ValueError(
                    "Google auth not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON for service-account auth, "
                    "or set EMAILGENIUS_GOOGLE_OAUTH_CLIENT_ID/EMAILGENIUS_GOOGLE_OAUTH_CLIENT_SECRET for OAuth."
                )
            client_id = input("Google OAuth client id: ").strip()
            client_secret = getpass.getpass("Google OAuth client secret: ").strip()
            if not (client_id and client_secret):
                raise ValueError("Missing Google OAuth client id/secret")

        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, scopes=resolved_scopes)
        creds = flow.run_local_server(port=oauth_local_port(), open_browser=True)
        _save_oauth_credentials(creds, token_path)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_oauth_credentials(creds, token_path)

    return creds
