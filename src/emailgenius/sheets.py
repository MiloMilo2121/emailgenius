from __future__ import annotations

from dataclasses import dataclass

import gspread


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


@dataclass(slots=True)
class SheetPublishResult:
    sheet_id: str
    worksheet: str
    rows_written: int


def publish_approval_rows(
    *,
    sheet_id: str,
    rows: list[dict[str, object]],
    service_account_json: str,
    output_schema: str = "ab",
    worksheet_name: str = "Drafts",
) -> SheetPublishResult:
    columns = approval_columns(output_schema)
    gc = gspread.service_account(filename=service_account_json)
    spreadsheet = gc.open_by_key(sheet_id)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=len(columns) + 5)

    header = worksheet.row_values(1)
    if header != columns:
        worksheet.clear()
        worksheet.append_row(columns)

    values: list[list[str]] = []
    for row in rows:
        values.append([_sheet_value(row.get(column)) for column in columns])

    if values:
        worksheet.append_rows(values, value_input_option="RAW")

    return SheetPublishResult(
        sheet_id=sheet_id,
        worksheet=worksheet_name,
        rows_written=len(values),
    )


def _sheet_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    return str(value)
