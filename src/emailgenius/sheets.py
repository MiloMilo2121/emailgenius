from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import gspread
from google.auth.transport.requests import AuthorizedSession

from .google_auth import oauth_local_port, oauth_token_path, resolve_google_credentials


APPROVAL_COLUMNS_AB = [
    "campaign_id",
    "parent_slug",
    "company_name",
    "contact_name",
    "contact_title",
    "contact_email",
    "SubjectLine",
    "Personalization",
    "CTA",
    "BodyTemplate",
    "Angle",
    "Trigger",
    "ResearchSources",
    "Source1",
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
    "SubjectLine",
    "Personalization",
    "CTA",
    "BodyTemplate",
    "Angle",
    "Trigger",
    "ResearchSources",
    "Source1",
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
    parent_slug: str | None,
    campaign_id: str | None,
    sheet_share_with: str | None,
    drive_folder_id: str | None,
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
        parent_slug=parent_slug,
        campaign_id=campaign_id,
    )
    if drive_folder_id:
        _move_sheet_to_drive_folder(gc=gc, sheet_id=resolved_id, folder_id_or_url=drive_folder_id)
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


def publish_master_status_rows(
    *,
    spreadsheet: gspread.Spreadsheet,
    rows: list[dict[str, object]],
    worksheet_name: str = "Status",
) -> int:
    columns = [
        "idempotency_key",
        "company_name",
        "contact_name",
        "status",
        "doc_url",
        "attack_angle",
        "recommended_next_step",
    ]
    return publish_rows_to_worksheet(
        spreadsheet=spreadsheet,
        worksheet_name=worksheet_name,
        columns=columns,
        rows=rows,
    )


def _resolve_gspread_client(*, service_account_json: str | None, interactive: bool) -> gspread.Client:
    creds = resolve_google_credentials(
        service_account_json=service_account_json,
        interactive=interactive,
        scopes=SHEETS_SCOPES,
    )
    return gspread.authorize(creds)


def _oauth_token_path() -> Path:
    return oauth_token_path()


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


def _oauth_local_port() -> int:
    return oauth_local_port()


def _open_or_create_spreadsheet(
    *,
    gc: gspread.Client,
    sheet_id: str | None,
    sheet_title: str | None,
    parent_slug: str | None,
    campaign_id: str | None,
) -> tuple[gspread.Spreadsheet, str]:
    if sheet_id:
        spreadsheet = gc.open_by_key(sheet_id)
        return spreadsheet, sheet_id
    title = _build_sheet_title(sheet_title=sheet_title, parent_slug=parent_slug, campaign_id=campaign_id)
    spreadsheet = gc.create(title)
    return spreadsheet, spreadsheet.id


def _build_sheet_title(*, sheet_title: str | None, parent_slug: str | None, campaign_id: str | None) -> str:
    base = (sheet_title or "EmailGenius Campaign").strip() or "EmailGenius Campaign"
    lowered = base.lower()
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parent_tag = " ".join((parent_slug or "").split()).strip()
    campaign_tag = ""
    compact_campaign_id = " ".join((campaign_id or "").split()).strip()
    if compact_campaign_id:
        campaign_tag = f"campaign-{compact_campaign_id[:8]}"

    parts = [base]
    if parent_tag and parent_tag.lower() not in lowered:
        parts.append(parent_tag)
    elif campaign_tag and campaign_tag.lower() not in lowered:
        parts.append(campaign_tag)

    if not re.search(r"\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b", base):
        parts.append(date_tag)

    title = " | ".join(part for part in parts if part)
    return title[:100]


def _move_sheet_to_drive_folder(*, gc: gspread.Client, sheet_id: str, folder_id_or_url: str) -> None:
    folder_id = _normalize_drive_folder_id(folder_id_or_url)
    if not folder_id:
        raise ValueError(f"Invalid Google Drive folder id/url: {folder_id_or_url}")

    auth = getattr(getattr(gc, "http_client", None), "auth", None)
    if auth is None:
        raise RuntimeError("Unable to resolve Google auth credentials from gspread client")
    session = AuthorizedSession(auth)
    file_url = f"https://www.googleapis.com/drive/v3/files/{sheet_id}"

    meta_resp = session.get(
        file_url,
        params={
            "fields": "id,parents",
            "supportsAllDrives": "true",
        },
        timeout=30,
    )
    if meta_resp.status_code >= 400:
        raise RuntimeError(f"Unable to read Google Drive file metadata (status={meta_resp.status_code})")

    payload = meta_resp.json() if meta_resp.content else {}
    parents = payload.get("parents") if isinstance(payload, dict) else []
    if not isinstance(parents, list):
        parents = []

    if folder_id in parents and len(parents) == 1:
        return

    patch_params: dict[str, str] = {
        "addParents": folder_id,
        "supportsAllDrives": "true",
        "fields": "id,parents",
    }
    to_remove = ",".join(parent for parent in parents if isinstance(parent, str) and parent and parent != folder_id)
    if to_remove:
        patch_params["removeParents"] = to_remove

    move_resp = session.patch(
        file_url,
        params=patch_params,
        timeout=30,
    )
    if move_resp.status_code >= 400:
        raise RuntimeError(f"Unable to move Google Sheet into Drive folder (status={move_resp.status_code})")


def _normalize_drive_folder_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "/" not in raw:
        return raw

    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""

    # Expected formats:
    # - https://drive.google.com/drive/folders/<id>
    # - https://drive.google.com/open?id=<id>
    parts = [item for item in parsed.path.split("/") if item]
    if "folders" in parts:
        idx = parts.index("folders")
        if idx + 1 < len(parts):
            return parts[idx + 1].strip()

    query = parse_qs(parsed.query)
    candidate = (query.get("id") or [""])[0].strip()
    return candidate


def _chunk(values: list[list[str]], size: int) -> Iterable[list[list[str]]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _sheet_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    return str(value)
