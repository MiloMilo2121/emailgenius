from __future__ import annotations

import hashlib
import io
import random
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import gspread
import yaml
try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except Exception:  # pragma: no cover - optional dependency in local test envs
    build = None  # type: ignore[assignment]
    HttpError = Exception  # type: ignore[assignment,misc]
    MediaIoBaseDownload = None  # type: ignore[assignment]

from .google_auth import DEFAULT_GOOGLE_SCOPES, resolve_google_credentials
from .knowledge import ingest_knowledge_file
from .leads import CANONICAL_HEADER_ALIASES
from .profiles import load_parent_profile
from .sheets import publish_master_status_rows
from .storage import PostgresStore
from .types import SequenceResult
from .utils import slugify

_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_DRIVE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_PARENTS_ROOT_NAMES = ("PARENTS", "Parents")
_PROFILE_FOLDER_NAMES = ("PROFILE", "Profile", "Profiles")
_KNOWLEDGE_FOLDER_NAMES = ("KNOWLEDGE", "Knowledge")
_LEADS_FOLDER_NAMES = ("LEADS", "Leads", "Input Leads")
_OUTPUT_FOLDER_NAMES = ("OUTPUT", "Output", "Output Sequences")


@dataclass(slots=True)
class WorkspaceClients:
    drive: Any
    docs: Any
    sheets: gspread.Client


@dataclass(slots=True)
class SyncReport:
    scanned: int = 0
    synced: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(slots=True)
class DriveLeadRow:
    raw_row: dict[str, str]
    canonical_row: dict[str, str]
    sheet_id: str
    sheet_name: str
    tab_name: str
    row_index: int
    modified_time: str
    idempotency_key: str


@dataclass(slots=True)
class ExportResult:
    docs_created: int
    status_rows_written: int
    doc_urls: list[str]


@dataclass(slots=True)
class ParentWorkspace:
    slug: str
    folder_id: str
    profile_folder_id: str | None = None
    knowledge_folder_id: str | None = None
    leads_folder_id: str | None = None
    output_folder_id: str | None = None


def build_workspace_clients(*, service_account_json: str | None, interactive: bool) -> WorkspaceClients:
    if build is None:
        raise RuntimeError(
            "google-api-python-client not installed. Install project dependencies to use io_mode=drive."
        )
    credentials = resolve_google_credentials(
        service_account_json=service_account_json,
        interactive=interactive,
        scopes=DEFAULT_GOOGLE_SCOPES,
    )
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    sheets = gspread.authorize(credentials)
    return WorkspaceClients(drive=drive, docs=docs, sheets=sheets)


def sync_parent_profiles(
    folder_id: str,
    store: PostgresStore,
    drive_client: Any,
    parent_slug: str | None = None,
) -> SyncReport:
    report = SyncReport()
    parent_workspaces = _discover_parent_workspaces(folder_id, drive_client, parent_slug=parent_slug)
    if parent_workspaces:
        for workspace in parent_workspaces:
            if not workspace.profile_folder_id:
                report.skipped += 1
                continue
            report = _sync_parent_profiles_from_folder(
                report=report,
                store=store,
                drive_client=drive_client,
                folder_id=workspace.profile_folder_id,
                slug_override=workspace.slug,
            )
        return report

    profiles_folder_id = _ensure_subfolder(drive_client, parent_id=folder_id, name="Profiles", create=False)
    if not profiles_folder_id:
        return report

    return _sync_parent_profiles_from_folder(
        report=report,
        store=store,
        drive_client=drive_client,
        folder_id=profiles_folder_id,
        slug_override=None,
    )


def _sync_parent_profiles_from_folder(
    *,
    report: SyncReport,
    store: PostgresStore,
    drive_client: Any,
    folder_id: str,
    slug_override: str | None,
) -> SyncReport:
    files = _list_files_in_folder(drive_client, folder_id)
    report.scanned += len(files)
    for item in files:
        name = str(item.get("name") or "")
        if not name.lower().endswith((".yaml", ".yml")):
            report.skipped += 1
            continue
        try:
            content = _download_drive_bytes(drive_client, str(item["id"]))
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".yaml") as handle:
                handle.write(content)
                handle.flush()
                profile = load_parent_profile(handle.name, slug_override=slug_override)
            store.upsert_parent_profile(profile)
            report.synced += 1
        except Exception:
            report.failed += 1
    return report


def sync_knowledge_base(
    folder_id: str,
    store: PostgresStore,
    llm,
    drive_client: Any,
    parent_slug: str | None = None,
) -> SyncReport:
    report = SyncReport()
    parent_workspaces = _discover_parent_workspaces(folder_id, drive_client, parent_slug=parent_slug)
    if parent_workspaces:
        for workspace in parent_workspaces:
            if not workspace.knowledge_folder_id:
                report.skipped += 1
                continue
            processed_folder_id = _ensure_subfolder(
                drive_client,
                parent_id=workspace.knowledge_folder_id,
                name="Processed",
                create=True,
            )
            report = _sync_knowledge_folder(
                report=report,
                folder_id=workspace.knowledge_folder_id,
                processed_folder_id=processed_folder_id,
                store=store,
                llm=llm,
                drive_client=drive_client,
                parent_slug=workspace.slug,
            )
        return report

    knowledge_folder_id = _ensure_subfolder(drive_client, parent_id=folder_id, name="Knowledge", create=False)
    if not knowledge_folder_id:
        return report

    processed_folder_id = _ensure_subfolder(drive_client, parent_id=knowledge_folder_id, name="Processed", create=True)
    return _sync_knowledge_folder(
        report=report,
        folder_id=knowledge_folder_id,
        processed_folder_id=processed_folder_id,
        store=store,
        llm=llm,
        drive_client=drive_client,
        parent_slug=None,
    )


def _sync_knowledge_folder(
    *,
    report: SyncReport,
    folder_id: str,
    processed_folder_id: str | None,
    store: PostgresStore,
    llm,
    drive_client: Any,
    parent_slug: str | None,
) -> SyncReport:
    files = _list_files_in_folder(drive_client, folder_id)
    report.scanned += len(files)
    known_parent_slugs = {str(item.slug).strip() for item in store.list_parent_profiles() if str(item.slug).strip()}
    active_parent_slug = store.get_active_parent_slug()
    for item in files:
        file_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        modified_time = str(item.get("modifiedTime") or "")
        suffix = Path(name).suffix.lower()
        if suffix not in {".pdf", ".docx", ".md", ".markdown", ".txt"}:
            report.skipped += 1
            continue

        if store.is_drive_file_synced(file_id=file_id, modified_time=modified_time, kind="knowledge"):
            report.skipped += 1
            continue

        resolved_parent_slug = parent_slug or _resolve_parent_slug_for_knowledge_file(
            file_name=name,
            known_parent_slugs=known_parent_slugs,
            active_parent_slug=active_parent_slug,
        )
        if not resolved_parent_slug:
            store.upsert_drive_file_sync_state(
                file_id=file_id,
                modified_time=modified_time,
                kind="knowledge",
                status="FAILED_PARENT_MAPPING",
            )
            report.failed += 1
            continue

        try:
            payload = _download_drive_bytes(drive_client, file_id)
            with tempfile.TemporaryDirectory() as tmpdir:
                local_path = Path(tmpdir) / name
                local_path.write_bytes(payload)
                ingest_knowledge_file(
                    store=store,
                    llm=llm,
                    parent_slug=resolved_parent_slug,
                    file_path=str(local_path),
                    kind="marketing",
                )
            if processed_folder_id:
                _move_file_to_folder(drive_client, file_id=file_id, target_folder_id=processed_folder_id)
            store.upsert_drive_file_sync_state(
                file_id=file_id,
                modified_time=modified_time,
                kind="knowledge",
                status="SYNCED",
            )
            report.synced += 1
        except Exception:
            store.upsert_drive_file_sync_state(
                file_id=file_id,
                modified_time=modified_time,
                kind="knowledge",
                status="FAILED",
            )
            report.failed += 1

    return report


def fetch_leads_sheet(
    folder_id: str,
    sheets_client: gspread.Client,
    drive_client: Any,
    parent_slug: str | None = None,
) -> list[DriveLeadRow]:
    parent_workspaces = _discover_parent_workspaces(folder_id, drive_client, parent_slug=parent_slug)
    if parent_workspaces:
        out: list[DriveLeadRow] = []
        for workspace in parent_workspaces:
            if not workspace.leads_folder_id:
                continue
            out.extend(
                _fetch_leads_from_folder(
                    folder_id=workspace.leads_folder_id,
                    sheets_client=sheets_client,
                    drive_client=drive_client,
                    forced_parent_slug=workspace.slug,
                )
            )
        return out

    input_folder_id = _ensure_subfolder(drive_client, parent_id=folder_id, name="Input Leads", create=False)
    if not input_folder_id:
        return []

    return _fetch_leads_from_folder(
        folder_id=input_folder_id,
        sheets_client=sheets_client,
        drive_client=drive_client,
        forced_parent_slug=None,
    )


def _fetch_leads_from_folder(
    *,
    folder_id: str,
    sheets_client: gspread.Client,
    drive_client: Any,
    forced_parent_slug: str | None,
) -> list[DriveLeadRow]:
    out: list[DriveLeadRow] = []
    files = _list_files_in_folder(drive_client, folder_id)
    for item in files:
        mime_type = str(item.get("mimeType") or "")
        if mime_type != _DRIVE_SHEET_MIME:
            continue
        sheet_id = str(item.get("id") or "")
        sheet_name = str(item.get("name") or "")
        modified_time = str(item.get("modifiedTime") or "")

        try:
            spreadsheet = sheets_client.open_by_key(sheet_id)
        except Exception:
            continue

        for worksheet in spreadsheet.worksheets():
            values = worksheet.get_all_values()
            if not values:
                continue
            headers = [str(value or "").strip() for value in values[0]]
            if not any(headers):
                continue

            for idx, row_values in enumerate(values[1:], start=2):
                raw_row = _row_from_values(headers=headers, values=row_values)
                if not _row_has_any_data(raw_row):
                    continue
                canonical_row = _canonicalize_lead_row(raw_row)
                if forced_parent_slug:
                    canonical_row["parent_slug"] = forced_parent_slug
                identity = f"{sheet_id}:{worksheet.title}:{idx}:{modified_time}"
                idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                out.append(
                    DriveLeadRow(
                        raw_row=raw_row,
                        canonical_row=canonical_row,
                        sheet_id=sheet_id,
                        sheet_name=sheet_name,
                        tab_name=worksheet.title,
                        row_index=idx,
                        modified_time=modified_time,
                        idempotency_key=idempotency_key,
                    )
                )
    return out


def export_sequence_to_drive(
    folder_id: str,
    data: list[dict[str, object]],
    docs_client: Any,
    drive_client: Any,
    sheets_client: gspread.Client,
    parent_slug: str | None = None,
) -> ExportResult:
    parent_workspaces = _discover_parent_workspaces(folder_id, drive_client, parent_slug=parent_slug)
    workspace_by_slug = {item.slug: item for item in parent_workspaces}
    global_output_folder_id = _ensure_subfolder(drive_client, parent_id=folder_id, name="Output Sequences", create=True)
    status_sheet = _open_or_create_master_status_sheet(
        drive_client=drive_client,
        sheets_client=sheets_client,
        parent_folder_id=folder_id,
    )

    status_rows: list[dict[str, object]] = []
    doc_urls: list[str] = []
    docs_created = 0

    for item in data:
        sequence = item.get("sequence_result")
        if not isinstance(sequence, SequenceResult):
            continue

        company_name = str(item.get("company_name") or "Azienda")
        contact_name = str(item.get("contact_name") or "Contatto")
        idempotency_key = str(item.get("idempotency_key") or "")
        status = str(item.get("generation_status") or "OK")
        parent_slug = str(item.get("parent_slug") or "").strip()

        title = f"{company_name} - {contact_name} - {idempotency_key[:8]}".strip(" -")
        doc_id = _create_google_doc(docs_client=docs_client, title=title)
        body_text = _render_sequence_doc(
            company_name=company_name,
            contact_name=contact_name,
            sequence=sequence,
        )
        _replace_doc_body(docs_client=docs_client, doc_id=doc_id, text=body_text)
        target_folder_id = global_output_folder_id
        if parent_slug and parent_slug in workspace_by_slug:
            workspace = workspace_by_slug[parent_slug]
            if not workspace.output_folder_id:
                workspace.output_folder_id = _ensure_subfolder_alias(
                    drive_client,
                    parent_id=workspace.folder_id,
                    names=_OUTPUT_FOLDER_NAMES,
                    create=True,
                )
            target_folder_id = workspace.output_folder_id or target_folder_id
        if target_folder_id:
            _move_file_to_folder(drive_client, file_id=doc_id, target_folder_id=target_folder_id)

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        doc_urls.append(doc_url)
        docs_created += 1

        status_rows.append(
            {
                "idempotency_key": idempotency_key,
                "company_name": company_name,
                "contact_name": contact_name,
                "status": status,
                "doc_url": doc_url,
                "attack_angle": sequence.attack_angle,
                "recommended_next_step": sequence.recommended_next_step or "",
            }
        )

    status_rows_written = publish_master_status_rows(
        spreadsheet=status_sheet,
        rows=status_rows,
        worksheet_name="Status",
    )

    return ExportResult(
        docs_created=docs_created,
        status_rows_written=status_rows_written,
        doc_urls=doc_urls,
    )


def _execute_with_backoff(callable_fn: Callable[[], Any], *, max_attempts: int = 5) -> Any:
    attempt = 0
    while True:
        try:
            return callable_fn()
        except HttpError as exc:
            status = int(getattr(exc, "status_code", 0) or getattr(getattr(exc, "resp", None), "status", 0) or 0)
            retryable = status in {429, 500, 502, 503, 504}
            if not retryable or attempt >= max_attempts - 1:
                raise
            delay = min(8.0, (2 ** attempt) + random.random())
            time.sleep(delay)
            attempt += 1


def _list_files_in_folder(drive_client: Any, folder_id: str) -> list[dict[str, object]]:
    q = f"'{folder_id}' in parents and trashed=false"
    response = _execute_with_backoff(
        lambda: drive_client.files()
        .list(
            q=q,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id,name,mimeType,modifiedTime,parents)",
            pageSize=1000,
        )
        .execute()
    )
    files = response.get("files") if isinstance(response, dict) else []
    if not isinstance(files, list):
        return []
    return [item for item in files if isinstance(item, dict)]


def _download_drive_bytes(drive_client: Any, file_id: str) -> bytes:
    if MediaIoBaseDownload is None:
        raise RuntimeError("google-api-python-client not installed")
    request = drive_client.files().get_media(fileId=file_id, supportsAllDrives=True)
    handle = io.BytesIO()
    downloader = MediaIoBaseDownload(handle, request)
    done = False
    while not done:
        _, done = _execute_with_backoff(lambda: downloader.next_chunk())
    return handle.getvalue()


def _ensure_subfolder(drive_client: Any, *, parent_id: str, name: str, create: bool) -> str | None:
    safe_name = name.replace("'", "\\'")
    q = (
        f"'{parent_id}' in parents and trashed=false and mimeType='{_DRIVE_FOLDER_MIME}' "
        f"and name='{safe_name}'"
    )
    response = _execute_with_backoff(
        lambda: drive_client.files()
        .list(
            q=q,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id,name)",
            pageSize=10,
        )
        .execute()
    )
    files = response.get("files") if isinstance(response, dict) else []
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            return str(first.get("id") or "") or None

    if not create:
        return None

    body = {
        "name": name,
        "mimeType": _DRIVE_FOLDER_MIME,
        "parents": [parent_id],
    }
    created = _execute_with_backoff(
        lambda: drive_client.files()
        .create(body=body, fields="id", supportsAllDrives=True)
        .execute()
    )
    return str(created.get("id") or "") if isinstance(created, dict) else None


def _ensure_subfolder_alias(
    drive_client: Any,
    *,
    parent_id: str,
    names: tuple[str, ...],
    create: bool,
) -> str | None:
    for name in names:
        folder_id = _ensure_subfolder(drive_client, parent_id=parent_id, name=name, create=False)
        if folder_id:
            return folder_id
    if not create or not names:
        return None
    return _ensure_subfolder(drive_client, parent_id=parent_id, name=names[0], create=True)


def _move_file_to_folder(drive_client: Any, *, file_id: str, target_folder_id: str) -> None:
    meta = _execute_with_backoff(
        lambda: drive_client.files()
        .get(fileId=file_id, fields="id,parents", supportsAllDrives=True)
        .execute()
    )
    parents = meta.get("parents") if isinstance(meta, dict) else []
    if not isinstance(parents, list):
        parents = []
    remove_parents = ",".join(item for item in parents if isinstance(item, str) and item and item != target_folder_id)
    _execute_with_backoff(
        lambda: drive_client.files()
        .update(
            fileId=file_id,
            addParents=target_folder_id,
            removeParents=remove_parents,
            supportsAllDrives=True,
            fields="id,parents",
        )
        .execute()
    )


def _discover_parent_workspaces(
    folder_id: str,
    drive_client: Any,
    *,
    parent_slug: str | None = None,
) -> list[ParentWorkspace]:
    parents_root_id = _ensure_subfolder_alias(
        drive_client,
        parent_id=folder_id,
        names=_PARENTS_ROOT_NAMES,
        create=False,
    )
    if not parents_root_id:
        return []

    requested_slug = slugify(parent_slug or "")
    workspaces: list[ParentWorkspace] = []
    for item in _list_files_in_folder(drive_client, parents_root_id):
        if str(item.get("mimeType") or "") != _DRIVE_FOLDER_MIME:
            continue
        raw_name = str(item.get("name") or "").strip()
        if not raw_name or raw_name.startswith(".") or raw_name.startswith("_"):
            continue
        slug = slugify(raw_name)
        if requested_slug and slug != requested_slug:
            continue
        workspace_folder_id = str(item.get("id") or "")
        if not workspace_folder_id:
            continue
        workspaces.append(
            ParentWorkspace(
                slug=slug,
                folder_id=workspace_folder_id,
                profile_folder_id=_ensure_subfolder_alias(
                    drive_client,
                    parent_id=workspace_folder_id,
                    names=_PROFILE_FOLDER_NAMES,
                    create=False,
                ),
                knowledge_folder_id=_ensure_subfolder_alias(
                    drive_client,
                    parent_id=workspace_folder_id,
                    names=_KNOWLEDGE_FOLDER_NAMES,
                    create=False,
                ),
                leads_folder_id=_ensure_subfolder_alias(
                    drive_client,
                    parent_id=workspace_folder_id,
                    names=_LEADS_FOLDER_NAMES,
                    create=False,
                ),
                output_folder_id=_ensure_subfolder_alias(
                    drive_client,
                    parent_id=workspace_folder_id,
                    names=_OUTPUT_FOLDER_NAMES,
                    create=False,
                ),
            )
        )

    return sorted(workspaces, key=lambda item: item.slug)


def _resolve_parent_slug_for_knowledge_file(
    *,
    file_name: str,
    known_parent_slugs: set[str],
    active_parent_slug: str | None,
) -> str | None:
    normalized_known = {slugify(item) for item in known_parent_slugs if item}
    if not normalized_known:
        active = slugify(active_parent_slug or "")
        return active or None

    stem = Path(file_name).stem.strip()
    candidates: list[str] = []

    if "__" in stem:
        candidates.append(stem.split("__", 1)[0])
    bracket_match = re.match(r"^\[([^\]]+)\]", stem)
    if bracket_match:
        candidates.append(bracket_match.group(1))
    candidates.append(stem)

    for candidate in candidates:
        slug = slugify(candidate)
        if slug in normalized_known:
            return slug

    if len(normalized_known) == 1:
        return next(iter(normalized_known))

    return None


def _row_from_values(*, headers: list[str], values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for index, key in enumerate(headers):
        if not key:
            continue
        value = values[index] if index < len(values) else ""
        out[key] = str(value or "").strip()
    return out


def _row_has_any_data(row: dict[str, str]) -> bool:
    return any((value or "").strip() for value in row.values())


def _canonicalize_lead_row(row: dict[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    normalized_items = {_normalize_key(key): (value or "") for key, value in row.items()}

    for target, aliases in CANONICAL_HEADER_ALIASES.items():
        value = ""
        for alias in aliases:
            hit = normalized_items.get(_normalize_key(alias))
            if hit:
                value = hit.strip()
                break
        canonical[target] = value

    if not canonical.get("Company Name") and canonical.get("Cleaned Company Name"):
        canonical["Company Name"] = canonical["Cleaned Company Name"]
    if not canonical.get("Cleaned Company Name") and canonical.get("Company Name"):
        canonical["Cleaned Company Name"] = canonical["Company Name"]
    if not canonical.get("Full Name"):
        canonical["Full Name"] = (
            f"{canonical.get('First Name', '').strip()} {canonical.get('Last Name', '').strip()}".strip()
        )

    if canonical.get("Lead City") in {"0", "-", "n/a", "N/A"}:
        canonical["Lead City"] = ""

    parent_slug = ""
    for key in ("parent_slug", "Parent Slug", "parentSlug"):
        value = (row.get(key) or "").strip()
        if value:
            parent_slug = slugify(value)
            break
    if parent_slug:
        canonical["parent_slug"] = parent_slug

    return canonical


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _open_or_create_master_status_sheet(*, drive_client: Any, sheets_client: gspread.Client, parent_folder_id: str):
    status_name = "EmailGenius Master Status"
    query = (
        f"'{parent_folder_id}' in parents and trashed=false and mimeType='{_DRIVE_SHEET_MIME}' "
        f"and name='{status_name}'"
    )
    response = _execute_with_backoff(
        lambda: drive_client.files()
        .list(
            q=query,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id,name)",
            pageSize=10,
        )
        .execute()
    )
    files = response.get("files") if isinstance(response, dict) else []
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            return sheets_client.open_by_key(str(first.get("id") or ""))

    spreadsheet = sheets_client.create(status_name)
    _move_file_to_folder(drive_client, file_id=spreadsheet.id, target_folder_id=parent_folder_id)
    return spreadsheet


def _create_google_doc(*, docs_client: Any, title: str) -> str:
    payload = {"title": title}
    response = _execute_with_backoff(lambda: docs_client.documents().create(body=payload).execute())
    if not isinstance(response, dict):
        raise RuntimeError("Unable to create Google Doc")
    doc_id = str(response.get("documentId") or "")
    if not doc_id:
        raise RuntimeError("Unable to resolve Google Doc id")
    return doc_id


def _replace_doc_body(*, docs_client: Any, doc_id: str, text: str) -> None:
    requests = [
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": 1,
                    "endIndex": 2,
                }
            }
        },
        {
            "insertText": {
                "location": {"index": 1},
                "text": text,
            }
        },
    ]
    _execute_with_backoff(
        lambda: docs_client.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
    )


def _render_sequence_doc(*, company_name: str, contact_name: str, sequence: SequenceResult) -> str:
    lines = [
        f"Azienda: {company_name}",
        f"Contatto: {contact_name}",
        f"Attack Angle: {sequence.attack_angle}",
        "",
    ]
    if sequence.trigger_facts:
        lines.append("Trigger Facts:")
        for fact in sequence.trigger_facts:
            lines.append(f"- {fact}")
        lines.append("")

    for step in sequence.steps:
        lines.extend(
            [
                f"[{step.step_id}] {step.goal}",
                f"Subject: {step.subject}",
                step.body,
                "",
            ]
        )

    if sequence.recommended_next_step:
        lines.append(f"Recommended Next Step: {sequence.recommended_next_step}")
    if sequence.global_risk_flags:
        lines.append("Risk Flags: " + "; ".join(sequence.global_risk_flags))

    return "\n".join(lines).strip() + "\n"
