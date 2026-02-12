from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import app_home


APPROVAL_COLUMNS_AB = [
    "campaign_id",
    "parent_slug",
    "company_name",
    "contact_name",
    "contact_title",
    "contact_email",
    "variant_a_subject",
    "variant_a_body",
    "variant_b_subject",
    "variant_b_body",
    "recommended_variant",
    "final_subject",
    "final_body",
    "selected_variant",
    "generation_status",
    "generation_warning",
    "error_code",
    "evidence_summary",
    "risk_flags",
    "status",
    "reviewer_notes",
    "approved_variant",
    "updated_at",
]


APPROVAL_COLUMNS_ABC = [
    "campaign_id",
    "parent_slug",
    "company_name",
    "contact_name",
    "contact_title",
    "contact_email",
    "variant_a_subject",
    "variant_a_body",
    "variant_b_subject",
    "variant_b_body",
    "variant_c_subject",
    "variant_c_body",
    "recommended_variant",
    "final_subject",
    "final_body",
    "selected_variant",
    "generation_status",
    "generation_warning",
    "error_code",
    "evidence_summary",
    "risk_flags",
    "status",
    "reviewer_notes",
    "approved_variant",
    "updated_at",
]


def approval_columns(output_schema: str = "ab") -> list[str]:
    if output_schema.lower() == "abc":
        return APPROVAL_COLUMNS_ABC
    return APPROVAL_COLUMNS_AB


SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass(slots=True)
class SheetPublishResult:
    sheet_id: str
    worksheet: str
    rows_written: int


@dataclass(slots=True)
class CampaignSheetPublishResult:
    sheet_id: str
    spreadsheet_url: str
    drafts_rows_written: int
    sendready_rows_written: int


def publish_campaign_to_sheets(
    *,
    sheet_id: str | None,
    sheet_title: str | None,
    sheet_share_with: str | None,
    rows: list[dict[str, object]],
    sendready_columns: list[str],
    service_account_json: str | None,
    output_schema: str = "ab",
    drafts_worksheet: str = "Drafts",
    sendready_worksheet: str = "SendReady",
    auth_interactive: bool = False,
) -> CampaignSheetPublishResult:
    gc = _resolve_gspread_client(service_account_json=service_account_json, interactive=auth_interactive)
    spreadsheet, resolved_id = _open_or_create_spreadsheet(
        gc=gc,
        sheet_id=sheet_id,
        sheet_title=sheet_title,
    )
    if sheet_share_with:
        spreadsheet.share(sheet_share_with, perm_type="user", role="writer", notify=False)

    drafts_columns = approval_columns(output_schema)
    drafts_written = publish_rows_to_worksheet(
        spreadsheet=spreadsheet,
        worksheet_name=drafts_worksheet,
        columns=drafts_columns,
        rows=rows,
    )
    sendready_written = publish_rows_to_worksheet(
        spreadsheet=spreadsheet,
        worksheet_name=sendready_worksheet,
        columns=sendready_columns,
        rows=rows,
    )
    return CampaignSheetPublishResult(
        sheet_id=resolved_id,
        spreadsheet_url=spreadsheet.url,
        drafts_rows_written=drafts_written,
        sendready_rows_written=sendready_written,
    )


def publish_approval_rows(
    *,
    sheet_id: str,
    rows: list[dict[str, object]],
    service_account_json: str,
    output_schema: str = "ab",
    worksheet_name: str = "Drafts",
) -> SheetPublishResult:
    columns = approval_columns(output_schema)
    gc = _resolve_gspread_client(service_account_json=service_account_json, interactive=False)
    spreadsheet = gc.open_by_key(sheet_id)
    rows_written = publish_rows_to_worksheet(
        spreadsheet=spreadsheet,
        worksheet_name=worksheet_name,
        columns=columns,
        rows=rows,
    )

    return SheetPublishResult(
        sheet_id=sheet_id,
        worksheet=worksheet_name,
        rows_written=rows_written,
    )


def publish_rows_to_worksheet(
    *,
    spreadsheet: gspread.Spreadsheet,
    worksheet_name: str,
    columns: list[str],
    rows: list[dict[str, object]],
    chunk_size: int = 500,
) -> int:
    if chunk_size <= 0:
        chunk_size = 500

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=max(2000, len(rows) + 20), cols=len(columns) + 5)

    header = worksheet.row_values(1)
    if header != columns:
        worksheet.clear()
        worksheet.append_row(columns)

    values: list[list[str]] = [[_sheet_value(row.get(column)) for column in columns] for row in rows]
    for chunk in _chunk(values, chunk_size):
        if chunk:
            worksheet.append_rows(chunk, value_input_option="RAW")
    return len(values)


def _resolve_gspread_client(*, service_account_json: str | None, interactive: bool) -> gspread.Client:
    if service_account_json:
        return gspread.service_account(filename=service_account_json)

    token_path = _oauth_token_path()
    creds = _load_oauth_credentials(token_path)
    if creds is None:
        client_id = _oauth_client_id()
        client_secret = _oauth_client_secret()
        if not (client_id and client_secret):
            if not interactive:
                raise ValueError(
                    "Google Sheets auth not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON for service-account auth, "
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
        flow = InstalledAppFlow.from_client_config(client_config, scopes=SHEETS_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        _save_oauth_credentials(creds, token_path)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_oauth_credentials(creds, token_path)

    return gspread.authorize(creds)


def _oauth_token_path() -> Path:
    override = (os.getenv("EMAILGENIUS_GOOGLE_OAUTH_TOKEN_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return app_home() / "google-oauth-token.json"


def _oauth_client_id() -> str | None:
    value = (os.getenv("EMAILGENIUS_GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    return value or None


def _oauth_client_secret() -> str | None:
    value = (
        os.getenv("EMAILGENIUS_GOOGLE_OAUTH_CLIENT_SECRET")
        or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        or ""
    ).strip()
    return value or None


def _load_oauth_credentials(path: Path) -> Credentials | None:
    if not path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(path), scopes=SHEETS_SCOPES)
    except Exception:
        return None


def _save_oauth_credentials(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


def _open_or_create_spreadsheet(
    *,
    gc: gspread.Client,
    sheet_id: str | None,
    sheet_title: str | None,
) -> tuple[gspread.Spreadsheet, str]:
    if sheet_id:
        spreadsheet = gc.open_by_key(sheet_id)
        return spreadsheet, sheet_id
    title = (sheet_title or "EmailGenius Campaign").strip() or "EmailGenius Campaign"
    spreadsheet = gc.create(title)
    return spreadsheet, spreadsheet.id


def _chunk(values: list[list[str]], size: int) -> Iterable[list[list[str]]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _sheet_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    return str(value)
