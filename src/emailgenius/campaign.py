from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .config import AppConfig
from .enrichment import build_enrichment_dossier_sync
from .leads import build_company_and_contacts, group_rows_by_company, read_leads_csv, select_primary_contact
from .llm import LLMGateway
from .sheets import APPROVAL_COLUMNS, publish_approval_rows
from .storage import PostgresStore
from .types import ApprovalRecord, CampaignCompanyResult, CampaignSummary, DraftEmailVariant
from .utils import utc_now_iso, write_csv


def run_campaign(
    *,
    config: AppConfig,
    store: PostgresStore,
    llm: LLMGateway,
    parent_slug: str,
    leads_csv_path: str,
    out_dir: str,
    sheet_id: str | None,
    stages: str = "all",
    headless: bool = True,
) -> tuple[CampaignSummary, Path, list[dict[str, object]]]:
    if stages != "all":
        raise ValueError("Current release supports only --stages all")

    parent = store.get_parent_profile(parent_slug)
    if parent is None:
        raise ValueError(f"Parent profile not found for slug: {parent_slug}")

    rows = read_leads_csv(leads_csv_path)
    groups = group_rows_by_company(rows)

    campaign_id = store.create_campaign(parent_slug=parent_slug, leads_file=leads_csv_path, sheet_id=sheet_id)

    export_rows: list[dict[str, object]] = []
    warnings_total = 0

    for company_rows in groups.values():
        company, contacts = build_company_and_contacts(company_rows)
        primary_contact = select_primary_contact(contacts)

        dossier, discovered_website = build_enrichment_dossier_sync(
            company=company,
            contact=primary_contact,
            headless=headless,
            max_extra_pages=2,
        )
        if discovered_website and not company.website:
            company.website = discovered_website

        retrieval_query = _build_retrieval_query(company=company, dossier=dossier)
        retrieval_embeddings = llm.embed_texts([retrieval_query])
        snippets: list[str] = []
        if retrieval_embeddings:
            search_results = store.search_knowledge_chunks(
                parent_slug=parent_slug,
                kind="marketing",
                query_embedding=retrieval_embeddings[0],
                top_k=6,
            )
            snippets = [str(item.get("content") or "") for item in search_results if item.get("content")]

        variants, recommended_variant, global_flags = llm.generate_campaign_variants(
            parent=parent,
            company=company,
            contact=primary_contact,
            dossier=dossier,
            marketing_snippets=snippets,
        )

        all_flags = sorted(set(global_flags + [flag for v in variants for flag in v.risk_flags]))
        if all_flags or not dossier.sources:
            warnings_total += 1
            if not dossier.sources:
                all_flags.append("limited_sources")

        result = CampaignCompanyResult(
            campaign_id=campaign_id,
            parent_slug=parent_slug,
            company=company,
            contact=primary_contact,
            dossier=dossier,
            variants=variants,
            recommended_variant=recommended_variant,
            approval=ApprovalRecord(status="PENDING", updated_at=utc_now_iso()),
            risk_flags=sorted(set(all_flags)),
        )
        store.insert_campaign_company_result(result)
        export_rows.append(_company_result_to_row(result))

    out_base = Path(out_dir)
    out_base.mkdir(parents=True, exist_ok=True)
    export_path = out_base / f"campaign-{campaign_id}.csv"
    write_csv(export_path, export_rows, APPROVAL_COLUMNS)

    if sheet_id and config.google_service_account_json:
        publish_approval_rows(
            sheet_id=sheet_id,
            rows=export_rows,
            service_account_json=config.google_service_account_json,
        )

    summary = CampaignSummary(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        leads_file=leads_csv_path,
        sheet_id=sheet_id,
        status="COMPLETED",
        companies_total=len(groups),
        generated_total=len(export_rows),
        warnings_total=warnings_total,
    )
    store.finalize_campaign(campaign_id, summary)
    store.purge_expired_campaign_data(config.retention_days)
    return summary, export_path, export_rows


def campaign_status(store: PostgresStore, campaign_id: str) -> dict[str, object] | None:
    summary = store.get_campaign_summary(campaign_id)
    if not summary:
        return None

    records = store.list_campaign_records(campaign_id)
    status_counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1

    summary["record_status_counts"] = status_counts
    summary["records_total"] = len(records)
    return summary


def export_campaign(store: PostgresStore, campaign_id: str, output_path: str) -> Path:
    records = store.list_campaign_records(campaign_id)
    rows: list[dict[str, object]] = []
    for record in records:
        payload = record.get("payload_json") or {}
        variants = payload.get("variants") if isinstance(payload, dict) else []
        by_name = {str(item.get("variant")).upper(): item for item in variants if isinstance(item, dict)}

        row = {
            "campaign_id": campaign_id,
            "parent_slug": record.get("parent_slug") or "",
            "company_name": record.get("company_name") or "",
            "contact_name": record.get("contact_name") or "",
            "contact_title": record.get("contact_title") or "",
            "contact_email": record.get("contact_email") or "",
            "variant_a_subject": by_name.get("A", {}).get("subject", ""),
            "variant_a_body": by_name.get("A", {}).get("body", ""),
            "variant_b_subject": by_name.get("B", {}).get("subject", ""),
            "variant_b_body": by_name.get("B", {}).get("body", ""),
            "variant_c_subject": by_name.get("C", {}).get("subject", ""),
            "variant_c_body": by_name.get("C", {}).get("body", ""),
            "recommended_variant": payload.get("recommended_variant", ""),
            "evidence_summary": "; ".join((payload.get("dossier", {}) or {}).get("evidence", [])[:5])
            if isinstance(payload, dict)
            else "",
            "risk_flags": "; ".join(payload.get("risk_flags", [])) if isinstance(payload, dict) else "",
            "status": record.get("status") or "PENDING",
            "reviewer_notes": record.get("reviewer_notes") or "",
            "approved_variant": record.get("approved_variant") or "",
            "updated_at": str(record.get("updated_at") or ""),
        }
        rows.append(row)

    target = Path(output_path)
    write_csv(target, rows, APPROVAL_COLUMNS)
    return target


def _build_retrieval_query(*, company, dossier) -> str:
    hints = [
        company.company_name,
        company.industry or "",
        company.keywords or "",
        " ".join(dossier.pain_hypotheses[:2]),
        " ".join(dossier.opportunity_hypotheses[:2]),
    ]
    return " | ".join(part for part in hints if part)


def _company_result_to_row(result: CampaignCompanyResult) -> dict[str, object]:
    variants = _variants_by_name(result.variants)
    evidence_summary = "; ".join(result.dossier.evidence[:5])
    row = {
        "campaign_id": result.campaign_id,
        "parent_slug": result.parent_slug,
        "company_name": result.company.company_name,
        "contact_name": result.contact.full_name if result.contact else "",
        "contact_title": result.contact.title if result.contact else "",
        "contact_email": result.contact.email if result.contact else "",
        "variant_a_subject": variants.get("A", {}).get("subject", ""),
        "variant_a_body": variants.get("A", {}).get("body", ""),
        "variant_b_subject": variants.get("B", {}).get("subject", ""),
        "variant_b_body": variants.get("B", {}).get("body", ""),
        "variant_c_subject": variants.get("C", {}).get("subject", ""),
        "variant_c_body": variants.get("C", {}).get("body", ""),
        "recommended_variant": result.recommended_variant,
        "evidence_summary": evidence_summary,
        "risk_flags": "; ".join(result.risk_flags),
        "status": result.approval.status,
        "reviewer_notes": result.approval.notes or "",
        "approved_variant": result.approval.approved_variant or "",
        "updated_at": result.approval.updated_at or utc_now_iso(),
    }
    return row


def _variants_by_name(variants: list[DraftEmailVariant]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for variant in variants:
        out[variant.variant.upper()] = asdict(variant)
    return out
