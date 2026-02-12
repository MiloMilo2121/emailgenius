from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import AppConfig
from .enrichment import build_enrichment_dossier_sync
from .leads import (
    format_header_mapping,
    group_rows_by_company,
    preflight_leads,
    read_leads_csv_detailed,
    select_primary_contact,
    build_company_and_contacts,
)
from .llm import LLMGateway
from .sheets import approval_columns, publish_approval_rows
from .storage import PostgresStore
from .types import ApprovalRecord, CampaignCompanyResult, CampaignSummary, DraftEmailVariant, EnrichmentDossier
from .utils import utc_now_iso, write_csv


@dataclass(slots=True)
class _RowOutcome:
    row_index: int
    export_row: dict[str, object]
    result: CampaignCompanyResult | None
    extra_payload: dict[str, object]
    warning: bool
    failed: bool
    fatal_error: bool
    error_message: str | None


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
    recipient_mode: str = "company",
    variant_mode: str = "ab",
    output_schema: str = "ab",
    llm_policy: str = "strict",
    enrichment_mode: str = "auto",
    max_concurrency: int = 5,
    max_retries: int = 3,
    backoff_base_seconds: float = 1.0,
    cost_cap_eur: float = 50.0,
    force_cost_override: bool = False,
) -> tuple[CampaignSummary, Path, list[dict[str, object]]]:
    if stages != "all":
        raise ValueError("Current release supports only --stages all")
    if recipient_mode not in {"company", "row"}:
        raise ValueError("recipient_mode must be one of: company, row")
    if variant_mode not in {"ab", "abc"}:
        raise ValueError("variant_mode must be one of: ab, abc")
    if output_schema not in {"ab", "abc"}:
        raise ValueError("output_schema must be one of: ab, abc")
    if llm_policy not in {"strict", "fallback"}:
        raise ValueError("llm_policy must be one of: strict, fallback")

    parent = store.get_parent_profile(parent_slug)
    if parent is None:
        raise ValueError(f"Parent profile not found for slug: {parent_slug}")

    csv_data = read_leads_csv_detailed(leads_csv_path)
    preflight = preflight_leads(csv_data)
    if preflight.rows_total == 0:
        raise ValueError("Leads CSV has no rows")

    print(f"[preflight] mapping: {format_header_mapping(preflight.header_mapping)}")
    print(
        f"[preflight] rows={preflight.rows_total} valid={preflight.rows_valid} "
        f"skipped={preflight.rows_skipped} required={','.join(preflight.required_fields)}"
    )

    estimated_cost_eur = _estimate_cost_eur(preflight.rows_valid)
    if estimated_cost_eur > cost_cap_eur and not force_cost_override:
        raise ValueError(
            f"Estimated campaign cost {estimated_cost_eur:.2f} EUR exceeds cap {cost_cap_eur:.2f} EUR. "
            "Use --force-cost-override to continue."
        )

    effective_enrichment_mode = _resolve_enrichment_mode(
        recipient_mode=recipient_mode,
        enrichment_mode=enrichment_mode,
    )
    rag_enabled = bool(config.openai_api_key)

    campaign_id = store.create_campaign(parent_slug=parent_slug, leads_file=leads_csv_path, sheet_id=sheet_id)
    all_columns = _merge_columns(preflight.input_columns, approval_columns(output_schema))
    warnings_total = 0
    rows_generated_ok = 0
    rows_failed = 0
    processed_companies = 0
    export_rows: list[dict[str, object]] = []

    if recipient_mode == "row":
        outcomes = _run_row_mode(
            campaign_id=campaign_id,
            parent_slug=parent_slug,
            parent=parent,
            preflight=preflight,
            store=store,
            llm=llm,
            variant_mode=variant_mode,
            llm_policy=llm_policy,
            rag_enabled=rag_enabled,
            effective_enrichment_mode=effective_enrichment_mode,
            headless=headless,
            max_concurrency=max_concurrency,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            output_schema=output_schema,
        )
        by_row_index = {item.row_index: item for item in outcomes}
        for row in preflight.rows:
            outcome = by_row_index.get(row.row_index)
            if outcome is None:
                export_rows.append(
                    _skipped_validation_row(
                        campaign_id=campaign_id,
                        parent_slug=parent_slug,
                        raw_row=row.raw_row,
                        missing_fields=row.missing_required,
                    )
                )
                warnings_total += 1
                continue

            if outcome.fatal_error:
                raise RuntimeError(outcome.error_message or "Fatal campaign error")
            export_rows.append(outcome.export_row)
            if outcome.warning:
                warnings_total += 1
            if outcome.failed:
                rows_failed += 1
            else:
                rows_generated_ok += 1
            if outcome.result is not None:
                store.insert_campaign_company_result(outcome.result, extra_payload=outcome.extra_payload)
                processed_companies += 1
    else:
        valid_rows = [item.row for item in preflight.rows if item.is_valid]
        groups = group_rows_by_company(valid_rows)
        for company_rows in groups.values():
            raw_row = _pick_raw_row_for_company(preflight.rows, company_rows[0])
            outcome = _process_company_like_item(
                campaign_id=campaign_id,
                parent_slug=parent_slug,
                parent=parent,
                canonical_rows=company_rows,
                raw_row=raw_row,
                store=store,
                llm=llm,
                variant_mode=variant_mode,
                llm_policy=llm_policy,
                rag_enabled=rag_enabled,
                effective_enrichment_mode=effective_enrichment_mode,
                headless=headless,
                max_retries=max_retries,
                backoff_base_seconds=backoff_base_seconds,
                output_schema=output_schema,
            )
            if outcome.fatal_error:
                raise RuntimeError(outcome.error_message or "Fatal campaign error")
            export_rows.append(outcome.export_row)
            if outcome.warning:
                warnings_total += 1
            if outcome.failed:
                rows_failed += 1
            else:
                rows_generated_ok += 1
            if outcome.result is not None:
                store.insert_campaign_company_result(outcome.result, extra_payload=outcome.extra_payload)
                processed_companies += 1

        for row in preflight.rows:
            if row.is_valid:
                continue
            warnings_total += 1
            export_rows.append(
                _skipped_validation_row(
                    campaign_id=campaign_id,
                    parent_slug=parent_slug,
                    raw_row=row.raw_row,
                    missing_fields=row.missing_required,
                )
            )

    # Preserve input row order when possible.
    if recipient_mode == "row":
        export_rows = _order_rows_like_input(export_rows, preflight.rows)

    out_base = Path(out_dir)
    out_base.mkdir(parents=True, exist_ok=True)
    export_path = out_base / f"campaign-{campaign_id}.csv"
    write_csv(export_path, export_rows, all_columns)

    if sheet_id and config.google_service_account_json:
        publish_approval_rows(
            sheet_id=sheet_id,
            rows=export_rows,
            output_schema=output_schema,
            service_account_json=config.google_service_account_json,
        )

    per_row_estimated_cost = estimated_cost_eur / max(preflight.rows_valid, 1)
    actual_cost_eur = round(per_row_estimated_cost * rows_generated_ok, 2)

    summary = CampaignSummary(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        leads_file=leads_csv_path,
        sheet_id=sheet_id,
        status="COMPLETED",
        companies_total=processed_companies,
        generated_total=rows_generated_ok,
        warnings_total=warnings_total,
        recipient_mode=recipient_mode,
        variant_mode=variant_mode,
        output_schema=output_schema,
        llm_policy=llm_policy,
        rows_total=preflight.rows_total,
        rows_valid=preflight.rows_valid,
        rows_skipped=preflight.rows_skipped,
        rows_generated_ok=rows_generated_ok,
        rows_failed=rows_failed,
        estimated_cost_eur=estimated_cost_eur,
        actual_cost_eur=actual_cost_eur,
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


def export_campaign(store: PostgresStore, campaign_id: str, output_path: str, output_schema: str = "auto") -> Path:
    records = store.list_campaign_records(campaign_id)
    summary = store.get_campaign_summary(campaign_id) or {}
    resolved_schema = _resolve_export_schema(output_schema=output_schema, summary=summary)
    columns = approval_columns(resolved_schema)

    rows: list[dict[str, object]] = []
    for record in records:
        payload = record.get("payload_json") or {}
        variants = payload.get("variants") if isinstance(payload, dict) else []
        by_name = {str(item.get("variant")).upper(): item for item in variants if isinstance(item, dict)}
        selected_variant = str(payload.get("selected_variant") or payload.get("recommended_variant") or "A").upper()

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
            "final_subject": by_name.get(selected_variant, {}).get("subject", ""),
            "final_body": by_name.get(selected_variant, {}).get("body", ""),
            "selected_variant": selected_variant,
            "generation_status": payload.get("generation_status", "OK"),
            "generation_warning": payload.get("generation_warning", ""),
            "error_code": payload.get("error_code", ""),
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
    write_csv(target, rows, columns)
    return target


def _run_row_mode(
    *,
    campaign_id: str,
    parent_slug: str,
    parent,
    preflight,
    store: PostgresStore,
    llm: LLMGateway,
    variant_mode: str,
    llm_policy: str,
    rag_enabled: bool,
    effective_enrichment_mode: str,
    headless: bool,
    max_concurrency: int,
    max_retries: int,
    backoff_base_seconds: float,
    output_schema: str,
) -> list[_RowOutcome]:
    valid_rows = [item for item in preflight.rows if item.is_valid]
    if not valid_rows:
        return []

    outcomes: list[_RowOutcome] = []
    workers = max(1, int(max_concurrency))
    total = len(valid_rows)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _process_company_like_item,
                campaign_id=campaign_id,
                parent_slug=parent_slug,
                parent=parent,
                canonical_rows=[item.row],
                raw_row=item.raw_row,
                store=store,
                llm=llm,
                variant_mode=variant_mode,
                llm_policy=llm_policy,
                rag_enabled=rag_enabled,
                effective_enrichment_mode=effective_enrichment_mode,
                headless=headless,
                max_retries=max_retries,
                backoff_base_seconds=backoff_base_seconds,
                output_schema=output_schema,
                row_index=item.row_index,
            )
            for item in valid_rows
        ]
        for future in as_completed(futures):
            outcome = future.result()
            outcomes.append(outcome)
            done += 1
            if done % 10 == 0 or done == total:
                print(f"[progress] generated {done}/{total}")
            if outcome.fatal_error:
                for pending in futures:
                    pending.cancel()
                break
    return outcomes


def _process_company_like_item(
    *,
    campaign_id: str,
    parent_slug: str,
    parent,
    canonical_rows: list[dict[str, str]],
    raw_row: dict[str, str],
    store: PostgresStore,
    llm: LLMGateway,
    variant_mode: str,
    llm_policy: str,
    rag_enabled: bool,
    effective_enrichment_mode: str,
    headless: bool,
    max_retries: int,
    backoff_base_seconds: float,
    output_schema: str,
    row_index: int = 0,
) -> _RowOutcome:
    company, contacts = build_company_and_contacts(canonical_rows)
    primary_contact = select_primary_contact(contacts)

    try:
        if effective_enrichment_mode == "minimal":
            dossier = _minimal_dossier(company_name=company.company_name)
            discovered_website = company.website
        else:
            dossier, discovered_website = build_enrichment_dossier_sync(
                company=company,
                contact=primary_contact,
                headless=headless,
                max_extra_pages=2,
            )
        if discovered_website and not company.website:
            company.website = discovered_website

        snippets: list[str] = []
        if rag_enabled:
            retrieval_query = _build_retrieval_query(company=company, dossier=dossier)
            retrieval_embeddings = llm.embed_texts([retrieval_query])
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
            variant_mode=variant_mode,
            llm_policy=llm_policy,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
        )
    except RuntimeError as exc:
        message = str(exc)
        if llm_policy == "strict" and (
            "LLM fatal error" in message or "LLM unavailable" in message
        ):
            return _RowOutcome(
                row_index=row_index,
                export_row={},
                result=None,
                extra_payload={},
                warning=True,
                failed=True,
                fatal_error=True,
                error_message=message,
            )

        error_code = "FAILED_LLM_RETRY_EXHAUSTED"
        export_row = _error_row(
            campaign_id=campaign_id,
            parent_slug=parent_slug,
            raw_row=raw_row,
            error_code=error_code,
            warning_message=message,
            output_schema=output_schema,
        )
        return _RowOutcome(
            row_index=row_index,
            export_row=export_row,
            result=None,
            extra_payload={},
            warning=True,
            failed=True,
            fatal_error=False,
            error_message=message,
        )

    all_flags = sorted(set(global_flags + [flag for v in variants for flag in v.risk_flags]))
    warning = bool(all_flags) or not dossier.sources
    if not dossier.sources:
        all_flags = sorted(set(all_flags + ["limited_sources"]))

    selected_variant = recommended_variant
    by_name = _variants_by_name(variants)
    final_subject = str(by_name.get(selected_variant, {}).get("subject") or "")
    final_body = str(by_name.get(selected_variant, {}).get("body") or "")
    generation_status = "OK"
    error_code = ""
    generation_warning = ""
    if any("failed_copy_guard" in flag for flag in all_flags):
        generation_status = "FAILED_COPY_GUARD"
        error_code = "FAILED_COPY_GUARD"
        generation_warning = "Copy guard non superato dopo repair"

    result = CampaignCompanyResult(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        company=company,
        contact=primary_contact,
        dossier=dossier,
        variants=variants,
        recommended_variant=recommended_variant,
        approval=ApprovalRecord(status="PENDING", updated_at=utc_now_iso()),
        risk_flags=all_flags,
    )

    export_row = _company_result_to_row(
        result=result,
        raw_row=raw_row,
        selected_variant=selected_variant,
        final_subject=final_subject,
        final_body=final_body,
        generation_status=generation_status,
        generation_warning=generation_warning,
        error_code=error_code,
        output_schema=output_schema,
    )
    extra_payload = {
        "selected_variant": selected_variant,
        "final_subject": final_subject,
        "final_body": final_body,
        "generation_status": generation_status,
        "generation_warning": generation_warning,
        "error_code": error_code,
        "raw_row": raw_row,
    }
    return _RowOutcome(
        row_index=row_index,
        export_row=export_row,
        result=result,
        extra_payload=extra_payload,
        warning=warning,
        failed=generation_status != "OK",
        fatal_error=False,
        error_message=None,
    )


def _resolve_enrichment_mode(*, recipient_mode: str, enrichment_mode: str) -> str:
    mode = enrichment_mode.lower()
    if mode == "auto":
        return "minimal" if recipient_mode == "row" else "web"
    if mode not in {"minimal", "hybrid", "web"}:
        raise ValueError("enrichment_mode must be one of: auto, minimal, hybrid, web")
    return mode


def _resolve_export_schema(*, output_schema: str, summary: dict[str, object]) -> str:
    mode = output_schema.lower()
    if mode in {"ab", "abc"}:
        return mode
    summary_json = summary.get("summary_json")
    if isinstance(summary_json, dict):
        maybe = str(summary_json.get("output_schema") or "").lower()
        if maybe in {"ab", "abc"}:
            return maybe
    return "ab"


def _estimate_cost_eur(rows_valid: int) -> float:
    estimated = rows_valid * 0.05
    return round(estimated, 2)


def _merge_columns(input_columns: list[str], generated_columns: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for column in input_columns + generated_columns:
        if column in seen:
            continue
        seen.add(column)
        out.append(column)
    return out


def _minimal_dossier(*, company_name: str) -> EnrichmentDossier:
    return EnrichmentDossier(
        site_summary=f"Dossier minimale generato da CSV per {company_name}.",
        pain_hypotheses=["allineamento priorita commerciali e execution operativa"],
        opportunity_hypotheses=["definire quick win con impatto commerciale tracciabile"],
        evidence=["Fonte primaria: CSV lead"],
        sources=["csv://lead-row"],
    )


def _build_retrieval_query(*, company, dossier) -> str:
    hints = [
        company.company_name,
        company.industry or "",
        company.keywords or "",
        " ".join(dossier.pain_hypotheses[:2]),
        " ".join(dossier.opportunity_hypotheses[:2]),
    ]
    return " | ".join(part for part in hints if part)


def _pick_raw_row_for_company(preflight_rows, company_row: dict[str, str]) -> dict[str, str]:
    company_name = (company_row.get("Company Name") or "").strip().lower()
    for item in preflight_rows:
        if not item.is_valid:
            continue
        if (item.row.get("Company Name") or "").strip().lower() == company_name:
            return item.raw_row
    return {}


def _order_rows_like_input(export_rows: list[dict[str, object]], preflight_rows) -> list[dict[str, object]]:
    by_email_company: dict[tuple[str, str], dict[str, object]] = {}
    tail: list[dict[str, object]] = []
    for row in export_rows:
        key = (str(row.get("Email") or ""), str(row.get("companyName") or row.get("company_name") or ""))
        if key in by_email_company:
            tail.append(row)
            continue
        by_email_company[key] = row

    ordered: list[dict[str, object]] = []
    for item in preflight_rows:
        key = (str(item.raw_row.get("Email") or ""), str(item.raw_row.get("companyName") or ""))
        match = by_email_company.pop(key, None)
        if match is not None:
            ordered.append(match)
    ordered.extend(by_email_company.values())
    ordered.extend(tail)
    return ordered


def _skipped_validation_row(
    *,
    campaign_id: str,
    parent_slug: str,
    raw_row: dict[str, str],
    missing_fields: list[str],
) -> dict[str, object]:
    warning = f"Missing required fields: {', '.join(missing_fields)}"
    company_name = raw_row.get("companyName") or raw_row.get("Company Name") or ""
    title = raw_row.get("jobTitle") or raw_row.get("Title") or ""
    contact_name = (
        raw_row.get("Full Name")
        or f"{raw_row.get('First Name', '')} {raw_row.get('Last Name', '')}".strip()
    )
    row = dict(raw_row)
    row.update(
        {
            "campaign_id": campaign_id,
            "parent_slug": parent_slug,
            "company_name": company_name,
            "contact_name": contact_name,
            "contact_title": title,
            "contact_email": raw_row.get("Email", "") or raw_row.get("email", ""),
            "recommended_variant": "",
            "final_subject": "",
            "final_body": "",
            "selected_variant": "",
            "generation_status": "SKIPPED_VALIDATION",
            "generation_warning": warning,
            "error_code": "SKIPPED_VALIDATION",
            "status": "PENDING",
            "updated_at": utc_now_iso(),
        }
    )
    return row


def _error_row(
    *,
    campaign_id: str,
    parent_slug: str,
    raw_row: dict[str, str],
    error_code: str,
    warning_message: str,
    output_schema: str,
) -> dict[str, object]:
    company_name = raw_row.get("companyName") or raw_row.get("Company Name") or ""
    title = raw_row.get("jobTitle") or raw_row.get("Title") or ""
    contact_name = (
        raw_row.get("Full Name")
        or f"{raw_row.get('First Name', '')} {raw_row.get('Last Name', '')}".strip()
    )
    row = dict(raw_row)
    row.update(
        {
            "campaign_id": campaign_id,
            "parent_slug": parent_slug,
            "company_name": company_name,
            "contact_name": contact_name,
            "contact_title": title,
            "contact_email": raw_row.get("Email", "") or raw_row.get("email", ""),
            "recommended_variant": "",
            "final_subject": "",
            "final_body": "",
            "selected_variant": "",
            "generation_status": "ERROR",
            "generation_warning": warning_message[:240],
            "error_code": error_code,
            "status": "PENDING",
            "updated_at": utc_now_iso(),
        }
    )
    if output_schema == "abc":
        row.setdefault("variant_c_subject", "")
        row.setdefault("variant_c_body", "")
    return row


def _company_result_to_row(
    *,
    result: CampaignCompanyResult,
    raw_row: dict[str, str],
    selected_variant: str,
    final_subject: str,
    final_body: str,
    generation_status: str,
    generation_warning: str,
    error_code: str,
    output_schema: str,
) -> dict[str, object]:
    variants = _variants_by_name(result.variants)
    evidence_summary = "; ".join(result.dossier.evidence[:5])
    row = dict(raw_row)
    row.update(
        {
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
            "recommended_variant": result.recommended_variant,
            "final_subject": final_subject,
            "final_body": final_body,
            "selected_variant": selected_variant,
            "generation_status": generation_status,
            "generation_warning": generation_warning,
            "error_code": error_code,
            "evidence_summary": evidence_summary,
            "risk_flags": "; ".join(result.risk_flags),
            "status": result.approval.status,
            "reviewer_notes": result.approval.notes or "",
            "approved_variant": result.approval.approved_variant or "",
            "updated_at": result.approval.updated_at or utc_now_iso(),
        }
    )
    if output_schema == "abc":
        row.update(
            {
                "variant_c_subject": variants.get("C", {}).get("subject", ""),
                "variant_c_body": variants.get("C", {}).get("body", ""),
            }
        )
    return row


def _variants_by_name(variants: list[DraftEmailVariant]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for variant in variants:
        out[variant.variant.upper()] = asdict(variant)
    return out
