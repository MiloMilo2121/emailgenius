from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .types import LeadCompany, LeadContact
from .utils import slugify


@dataclass(slots=True)
class LeadIngestResult:
    rows_total: int
    companies_total: int


@dataclass(slots=True)
class LeadCsvReadResult:
    rows: list[dict[str, str]]
    raw_rows: list[dict[str, str]]
    input_columns: list[str]
    header_mapping: dict[str, str]


@dataclass(slots=True)
class LeadPreflightRow:
    row_index: int
    row: dict[str, str]
    raw_row: dict[str, str]
    missing_required: list[str]
    is_valid: bool


@dataclass(slots=True)
class LeadPreflightResult:
    rows_total: int
    rows_valid: int
    rows_skipped: int
    required_fields: list[str]
    rows: list[LeadPreflightRow]
    input_columns: list[str]
    header_mapping: dict[str, str]


CANONICAL_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "First Name": ("First Name", "firstName", "firstname", "first_name"),
    "Last Name": ("Last Name", "lastName", "lastname", "last_name"),
    "Full Name": ("Full Name", "fullName", "fullname", "full_name"),
    "Title": ("Title", "jobTitle", "job_title", "role"),
    "Headline": ("Headline", "headline"),
    "Seniority": ("Seniority", "seniority"),
    "Email": ("Email", "email", "Email Address", "emailAddress"),
    "LinkedIn Link": ("LinkedIn Link", "linkedIn", "linkedin", "linkedin_link"),
    "Lead City": ("Lead City", "Company City", "city", "location"),
    "Lead State": ("Lead State", "Company State", "state", "province", "region"),
    "Lead Country": ("Lead Country", "Company Country", "country"),
    "Company Name": ("Company Name", "companyName", "company_name"),
    "Industry": ("Industry", "industry"),
    "Employee Count": ("Employee Count", "employees", "employeeCount", "employee_count"),
    "Cleaned Company Name": ("Cleaned Company Name", "cleanedCompanyName", "companyName"),
    "MillionVerifier Status": ("MillionVerifier Status", "Verification Status", "verificationStatus"),
    "Company Website Full": ("Company Website Full", "website", "Website", "companyWebsite"),
    "Company LinkedIn Link": ("Company LinkedIn Link", "companyLinkedIn", "company_linkedin"),
    "Company Keywords": ("Company Keywords", "keywords", "companyKeywords"),
    "Company Technologies": ("Company Technologies", "technologies", "companyTechnologies"),
    "Company Short Description": ("Company Short Description", "description", "Company Description"),
    "Company Founded Year": ("Company Founded Year", "founded", "foundedYear"),
    "Company Phone Number": ("Company Phone Number", "companyPhone", "phone"),
}

DEFAULT_REQUIRED_FIELDS = ("Email", "First Name", "Company Name", "Company Website Full")


def read_leads_csv(path: str | Path) -> list[dict[str, str]]:
    return read_leads_csv_detailed(path).rows


def read_leads_csv_detailed(path: str | Path) -> LeadCsvReadResult:
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        input_columns = [column for column in (reader.fieldnames or []) if column]
        normalized_to_original = {_normalize_key(column): column for column in input_columns}

        header_mapping: dict[str, str] = {}
        for canonical, aliases in CANONICAL_HEADER_ALIASES.items():
            for alias in aliases:
                hit = normalized_to_original.get(_normalize_key(alias))
                if hit:
                    header_mapping[canonical] = hit
                    break

        raw_rows: list[dict[str, str]] = []
        canonical_rows: list[dict[str, str]] = []
        for raw in reader:
            normalized_raw = {key: (value or "").strip() for key, value in raw.items() if key}
            raw_rows.append(normalized_raw)
            canonical_rows.append(_canonicalize_row(normalized_raw))

    return LeadCsvReadResult(
        rows=canonical_rows,
        raw_rows=raw_rows,
        input_columns=input_columns,
        header_mapping=header_mapping,
    )


def preflight_leads(
    data: LeadCsvReadResult,
    *,
    required_fields: tuple[str, ...] = DEFAULT_REQUIRED_FIELDS,
) -> LeadPreflightResult:
    rows: list[LeadPreflightRow] = []
    rows_valid = 0
    rows_skipped = 0

    for index, row in enumerate(data.rows, start=1):
        missing = [field for field in required_fields if not (row.get(field) or "").strip()]
        is_valid = not missing
        if is_valid:
            rows_valid += 1
        else:
            rows_skipped += 1

        rows.append(
            LeadPreflightRow(
                row_index=index,
                row=row,
                raw_row=data.raw_rows[index - 1] if index - 1 < len(data.raw_rows) else {},
                missing_required=missing,
                is_valid=is_valid,
            )
        )

    return LeadPreflightResult(
        rows_total=len(rows),
        rows_valid=rows_valid,
        rows_skipped=rows_skipped,
        required_fields=list(required_fields),
        rows=rows,
        input_columns=data.input_columns,
        header_mapping=data.header_mapping,
    )


def format_header_mapping(mapping: dict[str, str]) -> str:
    if not mapping:
        return "nessuna corrispondenza header rilevata"
    chunks = [f"{canonical} <- {source}" for canonical, source in sorted(mapping.items())]
    return "; ".join(chunks)


def group_rows_by_company(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        company_key = _company_key(row)
        groups[company_key].append(row)
    return dict(groups)


def build_company_and_contacts(company_rows: list[dict[str, str]]) -> tuple[LeadCompany, list[LeadContact]]:
    first = company_rows[0]
    company = LeadCompany(
        company_key=_company_key(first),
        company_name=_first_non_empty(first, ["Company Name", "Cleaned Company Name"]) or "Azienda",
        website=_clean_url(first.get("Company Website Full")),
        linkedin_company=_clean_url(first.get("Company LinkedIn Link")),
        industry=_empty_to_none(first.get("Industry")),
        employee_count=_parse_int(first.get("Employee Count")),
        location=_build_location(first),
        keywords=_empty_to_none(first.get("Company Keywords")),
        tech=_empty_to_none(first.get("Company Technologies")),
        founded_year=_parse_int(first.get("Company Founded Year")),
        evidence=_compact_company_evidence(first),
    )

    contacts = [build_contact(row) for row in company_rows]
    return company, contacts


def build_contact(row: dict[str, str]) -> LeadContact:
    full_name = _first_non_empty(row, ["Full Name"]) or (
        f"{row.get('First Name', '').strip()} {row.get('Last Name', '').strip()}".strip()
    )
    title = _empty_to_none(row.get("Title"))
    seniority = _empty_to_none(row.get("Seniority"))
    email = _empty_to_none(row.get("Email"))
    linkedin = _clean_url(row.get("LinkedIn Link"))
    quality_flag = _empty_to_none(row.get("MillionVerifier Status"))

    score = _contact_score(
        seniority=seniority,
        title=title,
        quality_flag=quality_flag,
        row=row,
    )

    return LeadContact(
        full_name=full_name or "Contatto",
        title=title,
        seniority=seniority,
        email=email,
        linkedin_person=linkedin,
        quality_flag=quality_flag,
        score=score,
        raw=row,
    )


def select_primary_contact(contacts: list[LeadContact]) -> LeadContact | None:
    if not contacts:
        return None
    sorted_contacts = sorted(contacts, key=lambda item: item.score, reverse=True)
    primary = sorted_contacts[0]
    for contact in sorted_contacts:
        contact.is_primary_contact = contact is primary
    return primary


def _canonicalize_row(row: dict[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    normalized_items = {_normalize_key(key): value for key, value in row.items()}

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

    return canonical


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _company_key(row: dict[str, str]) -> str:
    cleaned = (row.get("Cleaned Company Name") or "").strip()
    if cleaned:
        return slugify(cleaned)

    website = _clean_url(row.get("Company Website Full"))
    if website:
        host = urlparse(website).netloc.lower().replace("www.", "")
        if host:
            return slugify(host)

    fallback = (row.get("Company Name") or "azienda").strip()
    return slugify(fallback)


def _clean_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    return value


def _build_location(row: dict[str, str]) -> str | None:
    parts = [
        (row.get("Company City") or row.get("Lead City") or "").strip(),
        (row.get("Company State") or row.get("Lead State") or "").strip(),
        (row.get("Company Country") or row.get("Lead Country") or "").strip(),
    ]
    compact = [part for part in parts if part]
    if not compact:
        return None
    return ", ".join(compact)


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return None
    return int(digits)


def _first_non_empty(row: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return None


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _compact_company_evidence(row: dict[str, str]) -> list[str]:
    out: list[str] = []
    if row.get("Company Short Description"):
        out.append(row["Company Short Description"][:300])
    if row.get("Company Keywords"):
        out.append("Keywords disponibili")
    if row.get("Company Technologies"):
        out.append("Stack tecnologico disponibile")
    return out


def _contact_score(
    *,
    seniority: str | None,
    title: str | None,
    quality_flag: str | None,
    row: dict[str, str],
) -> float:
    score = 0.0

    seniority_rank = {
        "c_suite": 50,
        "founder": 45,
        "owner": 42,
        "executive": 38,
        "director": 34,
        "manager": 28,
        "mid": 16,
        "entry": 10,
    }
    if seniority:
        score += seniority_rank.get(seniority.lower(), 12)

    title_l = (title or "").lower()
    title_boosts = {
        "ceo": 20,
        "chief executive officer": 20,
        "amministratore delegato": 20,
        "founder": 18,
        "general manager": 16,
        "cfo": 14,
        "owner": 13,
    }
    for token, boost in title_boosts.items():
        if token in title_l:
            score += boost

    quality = (quality_flag or "").lower()
    if quality == "good":
        score += 10
    elif quality == "risky":
        score -= 5

    completeness_keys = [
        "Email",
        "LinkedIn Link",
        "Headline",
        "Title",
        "Seniority",
    ]
    completeness = sum(1 for key in completeness_keys if (row.get(key) or "").strip())
    score += completeness * 1.5

    return round(score, 2)
