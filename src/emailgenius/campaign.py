from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import threading
from urllib.parse import urlparse

from .agents import CampaignAgentEngine
from .config import AppConfig
from .enrichment import build_enrichment_dossier_sync, run_nebula_enrichment_machine
from .exa import ExaClient, _normalize_research_sources as _normalize_exa_research_sources
from .gdrive import (
    DriveLeadRow,
    build_workspace_clients,
    export_sequence_to_drive,
    fetch_leads_sheet,
    sync_knowledge_base,
    sync_parent_profiles,
)
from .guardrails import apply_claim_guard
from .leads import (
    format_header_mapping,
    group_rows_by_company,
    preflight_leads,
    read_leads_csv_detailed,
    select_primary_contact,
    build_company_and_contacts,
)
from .llm import (
    LLMGateway,
    format_email_body,
    format_email_subject,
    render_instantly_email,
    render_seed_template,
)
from .sheets import approval_columns, publish_campaign_to_sheets
from .storage import PostgresStore
from .types import (
    ApprovalRecord,
    CampaignCompanyResult,
    CampaignSummary,
    DraftEmailVariant,
    EnrichmentDossier,
    DEFAULT_RESEARCH_SOURCES,
    SearchHit,
    SequenceResult,
)
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
    leads_csv_path: str | None,
    out_dir: str,
    sheet_id: str | None,
    sheet_title: str | None = None,
    sheet_share_with: str | None = None,
    drive_folder_id: str | None = None,
    gsheets_auth: str = "auto",
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
    io_mode: str = "local",
    workspace_folder_id: str | None = None,
    research_sources: list[str] | None = None,
) -> tuple[CampaignSummary, Path, list[dict[str, object]]]:
    resolved_io_mode = (io_mode or "local").strip().lower()
    selected_research_sources = _normalize_exa_research_sources(
        research_sources,
        default_to=list(DEFAULT_RESEARCH_SOURCES),
    )
    if resolved_io_mode not in {"local", "drive"}:
        raise ValueError("io_mode must be one of: local, drive")

    if resolved_io_mode == "drive":
        return _run_campaign_drive_mode(
            config=config,
            store=store,
            llm=llm,
            parent_slug=parent_slug,
            out_dir=out_dir,
            sheet_id=sheet_id,
            sheet_title=sheet_title,
            gsheets_auth=gsheets_auth,
            headless=headless,
            variant_mode=variant_mode,
            output_schema=output_schema,
            llm_policy=llm_policy,
            enrichment_mode=enrichment_mode,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            cost_cap_eur=cost_cap_eur,
            force_cost_override=force_cost_override,
            workspace_folder_id=workspace_folder_id or config.workspace_folder_id,
            max_concurrency=max_concurrency,
            research_sources=selected_research_sources,
        )

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

    if not leads_csv_path:
        raise ValueError("leads_csv_path is required when io_mode=local")

    csv_data = read_leads_csv_detailed(leads_csv_path)
    preflight = preflight_leads(csv_data)
    if preflight.rows_total == 0:
        raise ValueError("Leads CSV has no rows")

    print(f"[preflight] mapping: {format_header_mapping(preflight.header_mapping)}")
    print(
        f"[preflight] rows={preflight.rows_total} valid={preflight.rows_valid} "
        f"skipped={preflight.rows_skipped} required={','.join(preflight.required_fields)}"
    )

    llm_items_planned = _estimate_llm_items_planned(preflight=preflight, recipient_mode=recipient_mode)
    estimated_cost_eur = _estimate_cost_eur(llm_items_planned)
    print(f"[preflight] llm_items_planned={llm_items_planned} (rows/companies with valid website)")
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
    research_client = ExaClient(config.exa_api_key)
    memory_snippets = _load_email_memory_snippets(store=store, parent_slug=parent_slug, limit=12)

    campaign_id = store.create_campaign(parent_slug=parent_slug, leads_file=leads_csv_path, sheet_id=sheet_id)
    all_columns = _merge_columns(preflight.input_columns, approval_columns(output_schema))
    warnings_total = 0
    rows_generated_ok = 0
    rows_failed = 0
    processed_companies = 0
    llm_items_attempted = 0
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
            recipient_mode=recipient_mode,
            max_concurrency=max_concurrency,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            output_schema=output_schema,
            memory_snippets=memory_snippets,
            research_client=research_client,
            research_sources=selected_research_sources,
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
            if bool(outcome.extra_payload.get("used_llm")):
                llm_items_attempted += 1
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
            if not raw_row:
                export_rows.append(
                    _error_row(
                        campaign_id=campaign_id,
                        parent_slug=parent_slug,
                        raw_row=company_rows[0],
                        error_code="RAW_ROW_MISSING",
                        warning_message="Impossibile trovare il dato sorgente per questa azienda",
                        output_schema=output_schema,
                    )
                )
                warnings_total += 1
                rows_failed += 1
                continue

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
                recipient_mode=recipient_mode,
                max_retries=max_retries,
                backoff_base_seconds=backoff_base_seconds,
                output_schema=output_schema,
                memory_snippets=memory_snippets,
                research_client=research_client,
                research_sources=selected_research_sources,
            )
            if outcome.fatal_error:
                raise RuntimeError(outcome.error_message or "Fatal campaign error")
            export_rows.append(outcome.export_row)
            if bool(outcome.extra_payload.get("used_llm")):
                llm_items_attempted += 1
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

    published_sheet_id = sheet_id
    if sheet_id or sheet_title:
        auth_mode = (gsheets_auth or "auto").lower()
        if auth_mode not in {"auto", "service_account", "oauth"}:
            raise ValueError("gsheets_auth must be one of: auto, service_account, oauth")

        service_account_json = config.google_service_account_json
        auth_interactive = False
        if auth_mode == "oauth":
            service_account_json = None
            auth_interactive = True
        elif auth_mode == "service_account":
            if not service_account_json:
                raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is required for gsheets_auth=service_account")
        else:
            # auto: use service account if configured, otherwise require explicit oauth mode
            if not service_account_json:
                raise ValueError(
                    "Google Sheets auth not configured. "
                    "Set GOOGLE_SERVICE_ACCOUNT_JSON or re-run with --gsheets-auth oauth."
                )

        publish_result = publish_campaign_to_sheets(
            sheet_id=sheet_id,
            sheet_title=sheet_title,
            parent_slug=parent_slug,
            campaign_id=campaign_id,
            sheet_share_with=sheet_share_with,
            drive_folder_id=drive_folder_id,
            rows=export_rows,
            sendready_columns=all_columns,
            service_account_json=service_account_json,
            output_schema=output_schema,
            auth_interactive=auth_interactive,
        )
        published_sheet_id = publish_result.sheet_id
        try:
            store.set_campaign_sheet_id(campaign_id, published_sheet_id)
        except Exception:
            # Non-critical: keep summary_json as source of truth.
            pass
        print(f"[gsheets] published: {publish_result.spreadsheet_url}")

    per_item_estimated_cost = estimated_cost_eur / max(llm_items_planned, 1)
    actual_cost_eur = round(per_item_estimated_cost * llm_items_attempted, 2) if llm_items_planned else 0.0

    _persist_email_memory_snapshot(
        store=store,
        parent_slug=parent_slug,
        summary_rows=export_rows,
        rows_total=preflight.rows_total,
        rows_generated_ok=rows_generated_ok,
        rows_failed=rows_failed,
    )

    summary = CampaignSummary(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        leads_file=leads_csv_path,
        sheet_id=published_sheet_id,
        status="COMPLETED",
        companies_total=processed_companies,
        generated_total=rows_generated_ok,
        warnings_total=warnings_total,
        recipient_mode=recipient_mode,
        variant_mode=variant_mode,
        output_schema=output_schema,
        llm_policy=llm_policy,
        io_mode="local",
        workspace_folder_id=None,
        rows_total=preflight.rows_total,
        rows_valid=preflight.rows_valid,
        rows_skipped=preflight.rows_skipped,
        rows_generated_ok=rows_generated_ok,
        rows_failed=rows_failed,
        estimated_cost_eur=estimated_cost_eur,
        actual_cost_eur=actual_cost_eur,
        research_sources=selected_research_sources,
    )
    store.finalize_campaign(campaign_id, summary)
    store.purge_expired_campaign_data(config.retention_days)
    return summary, export_path, export_rows


def _run_campaign_drive_mode(
    *,
    config: AppConfig,
    store: PostgresStore,
    llm: LLMGateway,
    parent_slug: str,
    out_dir: str,
    sheet_id: str | None,
    sheet_title: str | None,
    gsheets_auth: str,
    headless: bool,
    variant_mode: str,
    output_schema: str,
    llm_policy: str,
    enrichment_mode: str,
    max_retries: int,
    backoff_base_seconds: float,
    cost_cap_eur: float,
    force_cost_override: bool,
    workspace_folder_id: str | None,
    max_concurrency: int = 1,
    research_sources: list[str] | None = None,
) -> tuple[CampaignSummary, Path, list[dict[str, object]]]:
    if not workspace_folder_id:
        raise ValueError("workspace_folder_id is required when io_mode=drive")

    auth_mode = (gsheets_auth or "auto").lower()
    if auth_mode not in {"auto", "service_account", "oauth"}:
        raise ValueError("gsheets_auth must be one of: auto, service_account, oauth")

    service_account_json = config.google_service_account_json
    auth_interactive = auth_mode == "oauth"
    if auth_mode == "service_account" and not service_account_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is required for gsheets_auth=service_account")
    if auth_mode == "oauth":
        service_account_json = None
    if auth_mode == "auto" and not service_account_json:
        # Keep backward-compatible local behavior while enabling drive mode via OAuth when needed.
        auth_interactive = True

    clients = build_workspace_clients(
        service_account_json=service_account_json,
        interactive=auth_interactive,
    )

    profiles_report = sync_parent_profiles(workspace_folder_id, store, clients.drive, parent_slug=parent_slug)
    knowledge_report = sync_knowledge_base(workspace_folder_id, store, llm, clients.drive, parent_slug=parent_slug)
    lead_rows = fetch_leads_sheet(workspace_folder_id, clients.sheets, clients.drive, parent_slug=parent_slug)
    if not lead_rows:
        print("[drive-sync] lead_rows is empty, returning early")
        return CampaignSummary(
            campaign_id="",
            parent_slug=parent_slug,
            leads_file=f"drive://{workspace_folder_id}/Input Leads",
            sheet_id=sheet_id,
            status="COMPLETED",
            companies_total=0,
            generated_total=0,
            warnings_total=0,
            rows_total=0,
            rows_valid=0,
            rows_skipped=0,
            rows_generated_ok=0,
            rows_failed=0,
            estimated_cost_eur=0.0,
            actual_cost_eur=0.0,
        ), Path(out_dir) / "campaign-empty.csv", []

    llm_items_planned = len({item.idempotency_key for item in lead_rows})
    estimated_cost_eur = _estimate_cost_eur(llm_items_planned)
    print(f"[drive-preflight] llm_items_planned={llm_items_planned} (rows by idempotency key)")
    if estimated_cost_eur > cost_cap_eur and not force_cost_override:
        raise ValueError(
            f"Estimated campaign cost {estimated_cost_eur:.2f} EUR exceeds cap {cost_cap_eur:.2f} EUR. "
            "Use --force-cost-override to continue."
        )

    print(
        f"[drive-sync] profiles synced={profiles_report.synced} failed={profiles_report.failed} "
        f"knowledge synced={knowledge_report.synced} failed={knowledge_report.failed}"
    )
    print(f"[drive-sync] lead_rows={len(lead_rows)}")

    leads_ref = f"drive://{workspace_folder_id}/Input Leads"
    campaign_id = store.create_campaign(parent_slug=parent_slug, leads_file=leads_ref, sheet_id=sheet_id)

    checkpointer_builder = getattr(store, "build_langgraph_checkpointer", None)
    checkpointer = checkpointer_builder() if callable(checkpointer_builder) else None
    engine = CampaignAgentEngine(llm=llm, checkpointer=checkpointer)
    effective_enrichment_mode = _resolve_enrichment_mode(recipient_mode="row", enrichment_mode=enrichment_mode)
    rag_enabled = bool(config.openai_api_key)
    memory_snippets = _load_email_memory_snippets(store=store, parent_slug=parent_slug, limit=12)

    rows_total = len(lead_rows)
    rows_generated_ok = 0
    rows_failed = 0
    warnings_total = 0
    processed_companies = 0
    llm_items_attempted = 0
    export_rows: list[dict[str, object]] = []
    drive_export_items: list[dict[str, object]] = []

    workers = max(1, min(max_concurrency, 8))
    semaphore = threading.Semaphore(workers)
    _lock = threading.Lock()

    def _process_drive_row(lead_row: DriveLeadRow) -> None:
        nonlocal warnings_total, rows_failed, llm_items_attempted, rows_generated_ok, processed_companies
        with semaphore:
            if not store.begin_drive_row_ingestion(
                idempotency_key=lead_row.idempotency_key,
                sheet_id=lead_row.sheet_id,
                tab_name=lead_row.tab_name,
                row_index=lead_row.row_index,
                modified_time=lead_row.modified_time,
                campaign_id=campaign_id,
            ):
                return

            row_parent_slug = _resolve_parent_slug_for_drive_row(store=store, row=lead_row, fallback=parent_slug)
            row_parent = store.get_parent_profile(row_parent_slug) if row_parent_slug else None
            if row_parent is None:
                error_row = _error_row(
                    campaign_id=campaign_id,
                    parent_slug=row_parent_slug or parent_slug,
                    raw_row=lead_row.raw_row,
                    error_code="PARENT_PROFILE_NOT_FOUND",
                    warning_message="Parent profile non trovato per la riga Drive.",
                    output_schema=output_schema,
                )
                error_row["idempotency_key"] = lead_row.idempotency_key
                store.complete_drive_row_ingestion(
                    idempotency_key=lead_row.idempotency_key,
                    record_id=None,
                    status="FAILED",
                    error_message="Parent profile non trovato",
                )
                with _lock:
                    warnings_total += 1
                    rows_failed += 1
                    export_rows.append(error_row)
                return

            company, contacts = build_company_and_contacts([lead_row.canonical_row])
            primary_contact = select_primary_contact(contacts)

            try:
                if company.website and effective_enrichment_mode != "minimal":
                    dossier, discovered_website = build_enrichment_dossier_sync(
                        company=company,
                        contact=primary_contact,
                        headless=headless,
                        max_extra_pages=0,
                        snapshot_timeout_ms=18000,
                    )
                    if discovered_website and not company.website:
                        company.website = discovered_website
                else:
                    dossier = _minimal_dossier(company_name=company.company_name)

                nebula = run_nebula_enrichment_machine(
                    company=company,
                    contact=primary_contact,
                    dossier=dossier,
                )
                snippets = nebula.to_prompt_snippets(limit=10) + memory_snippets
                if rag_enabled:
                    retrieval_query = _build_retrieval_query(company=company, dossier=dossier)
                    retrieval_embeddings = llm.embed_texts([retrieval_query])
                    if retrieval_embeddings:
                        search_results = store.search_knowledge_chunks(
                            parent_slug=row_parent_slug,
                            kind="marketing",
                            query_embedding=retrieval_embeddings[0],
                            top_k=6,
                        )
                        snippets.extend(str(item.get("content") or "") for item in search_results if item.get("content"))
                snippets = _dedupe_snippets(snippets, limit=14)

                with _lock:
                    llm_items_attempted += 1

                sequence = engine.generate_sequence(
                    parent=row_parent,
                    company=company,
                    contact=primary_contact,
                    dossier=dossier,
                    marketing_snippets=snippets,
                    llm_policy=llm_policy,
                )
                risk_flags = sorted(
                    set(sequence.global_risk_flags + [flag for step in sequence.steps for flag in step.risk_flags])
                )

                result = CampaignCompanyResult(
                    campaign_id=campaign_id,
                    parent_slug=row_parent_slug,
                    company=company,
                    contact=primary_contact,
                    dossier=dossier,
                    variants=[],
                    recommended_variant="",
                    approval=ApprovalRecord(status="PENDING", updated_at=utc_now_iso()),
                    sequence_result=sequence,
                    risk_flags=risk_flags,
                )
                selected_step = next((item for item in sequence.steps if item.step_id.upper() == "E1"), None)
                final_subject = selected_step.subject if selected_step else ""
                final_body = selected_step.body if selected_step else ""
                generation_warning = ""
                if risk_flags:
                    generation_warning = "; ".join(risk_flags)[:240]
                    with _lock:
                        warnings_total += 1

                export_row = dict(lead_row.raw_row)
                export_row.update(
                    {
                        "campaign_id": campaign_id,
                        "parent_slug": row_parent_slug,
                        "company_name": company.company_name,
                        "contact_name": primary_contact.full_name if primary_contact else "",
                        "contact_title": primary_contact.title if primary_contact else "",
                        "contact_email": primary_contact.email if primary_contact else "",
                        "final_subject": final_subject,
                        "final_body": final_body,
                        "generation_status": "OK",
                        "generation_warning": generation_warning,
                        "error_code": "",
                        "risk_flags": "; ".join(risk_flags),
                        "status": "PENDING",
                        "updated_at": utc_now_iso(),
                        "idempotency_key": lead_row.idempotency_key,
                        "sequence_steps": len(sequence.steps),
                        "attack_angle": sequence.attack_angle,
                    }
                )
                record_id = store.insert_campaign_company_result(
                    result,
                    extra_payload={
                        "raw_row": lead_row.raw_row,
                        "idempotency_key": lead_row.idempotency_key,
                        "source_sheet_id": lead_row.sheet_id,
                        "source_tab_name": lead_row.tab_name,
                        "source_row_index": lead_row.row_index,
                        "source_modified_time": lead_row.modified_time,
                        "generation_status": "OK",
                        "generation_warning": generation_warning,
                        "error_code": "",
                    },
                )
                store.complete_drive_row_ingestion(
                    idempotency_key=lead_row.idempotency_key,
                    record_id=record_id,
                    status="COMPLETED",
                )
                with _lock:
                    export_rows.append(export_row)
                    drive_export_items.append(
                        {
                            "idempotency_key": lead_row.idempotency_key,
                            "parent_slug": row_parent_slug,
                            "company_name": company.company_name,
                            "contact_name": primary_contact.full_name if primary_contact else "",
                            "generation_status": "OK",
                            "sequence_result": sequence,
                        }
                    )
                    rows_generated_ok += 1
                    processed_companies += 1
            except Exception as exc:
                error_row = _error_row(
                    campaign_id=campaign_id,
                    parent_slug=row_parent_slug,
                    raw_row=lead_row.raw_row,
                    error_code="DRIVE_ROW_PROCESSING_FAILED",
                    warning_message=str(exc),
                    output_schema=output_schema,
                )
                error_row["idempotency_key"] = lead_row.idempotency_key
                store.complete_drive_row_ingestion(
                    idempotency_key=lead_row.idempotency_key,
                    record_id=None,
                    status="FAILED",
                    error_message=str(exc)[:500],
                )
                with _lock:
                    rows_failed += 1
                    warnings_total += 1
                    export_rows.append(error_row)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_process_drive_row, row) for row in lead_rows]
        for f in as_completed(futures):
            f.result()  # re-raise any unhandled exceptions from worker

    export_result = export_sequence_to_drive(
        workspace_folder_id,
        drive_export_items,
        clients.docs,
        clients.drive,
        clients.sheets,
        parent_slug=parent_slug,
    )
    print(
        f"[drive-export] docs_created={export_result.docs_created} "
        f"status_rows_written={export_result.status_rows_written}"
    )

    out_base = Path(out_dir)
    out_base.mkdir(parents=True, exist_ok=True)
    export_path = out_base / f"campaign-{campaign_id}.csv"
    
    base_cols = ["idempotency_key", "sequence_steps", "attack_angle"]
    if lead_rows:
        base_cols = list(lead_rows[0].raw_row.keys()) + base_cols
        
    columns = _merge_columns(
        base_cols,
        approval_columns(output_schema),
    )
    write_csv(export_path, export_rows, columns)

    per_item_estimated_cost = estimated_cost_eur / max(llm_items_planned, 1)
    actual_cost_eur = round(per_item_estimated_cost * llm_items_attempted, 2) if llm_items_planned else 0.0

    summary = CampaignSummary(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        leads_file=leads_ref,
        sheet_id=sheet_id,
        status="COMPLETED",
        companies_total=processed_companies,
        generated_total=rows_generated_ok,
        warnings_total=warnings_total,
        recipient_mode="row",
        variant_mode=variant_mode,
        output_schema=output_schema,
        llm_policy=llm_policy,
        io_mode="drive",
        workspace_folder_id=workspace_folder_id,
        rows_total=rows_total,
        rows_valid=rows_total,
        rows_skipped=0,
        rows_generated_ok=rows_generated_ok,
        rows_failed=rows_failed,
        estimated_cost_eur=estimated_cost_eur,
        actual_cost_eur=actual_cost_eur,
        research_sources=list(research_sources or []),
    )
    store.finalize_campaign(campaign_id, summary)
    store.purge_expired_campaign_data(config.retention_days)
    return summary, export_path, export_rows


def _resolve_parent_slug_for_drive_row(
    *,
    store: PostgresStore,
    row: DriveLeadRow,
    fallback: str,
) -> str:
    explicit = (row.canonical_row.get("parent_slug") or "").strip()
    if explicit:
        return explicit
    active = store.get_active_parent_slug()
    if active:
        return active
    if fallback:
        return fallback
    profiles = store.list_parent_profiles()
    if len(profiles) == 1:
        return profiles[0].slug
    return ""


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
    approval_cols = approval_columns(resolved_schema)

    rows: list[dict[str, object]] = []
    raw_columns: list[str] = []
    for record in records:
        payload = record.get("payload_json") or {}
        variants = payload.get("variants") if isinstance(payload, dict) else []
        by_name = {str(item.get("variant")).upper(): item for item in variants if isinstance(item, dict)}
        sequence_payload = payload.get("sequence_result") if isinstance(payload, dict) else None
        if not by_name and isinstance(sequence_payload, dict):
            steps = sequence_payload.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    step_id = str(step.get("step_id") or "").upper()
                    if step_id == "E1":
                        by_name["A"] = {
                            "variant": "A",
                            "subject": str(step.get("subject") or ""),
                            "body": str(step.get("body") or ""),
                            "risk_flags": step.get("risk_flags") or [],
                        }
                    elif step_id == "E2":
                        by_name["B"] = {
                            "variant": "B",
                            "subject": str(step.get("subject") or ""),
                            "body": str(step.get("body") or ""),
                            "risk_flags": step.get("risk_flags") or [],
                        }
                    elif step_id in {"E3", "BREAKUP"} and "C" not in by_name:
                        by_name["C"] = {
                            "variant": "C",
                            "subject": str(step.get("subject") or ""),
                            "body": str(step.get("body") or ""),
                            "risk_flags": step.get("risk_flags") or [],
                        }

        raw_row = payload.get("raw_row") if isinstance(payload, dict) else None
        row: dict[str, object] = dict(raw_row) if isinstance(raw_row, dict) else {}
        for key in row.keys():
            if key not in raw_columns:
                raw_columns.append(key)

        preferred_variant = str(payload.get("selected_variant") or payload.get("recommended_variant") or "A").strip().upper()
        approved_variant = str(record.get("approved_variant") or "").strip().upper()

        def _passes_copy_guard(item: dict[str, object]) -> bool:
            flags = item.get("risk_flags") or []
            return isinstance(flags, list) and "failed_copy_guard" not in flags

        passing = [name for name in ["A", "B", "C"] if name in by_name and _passes_copy_guard(by_name[name])]
        failed = [name for name in ["A", "B", "C"] if name in by_name and not _passes_copy_guard(by_name[name])]

        selected_variant = approved_variant if approved_variant and approved_variant in by_name else preferred_variant
        if selected_variant not in passing and passing:
            selected_variant = passing[0]
        if selected_variant not in by_name:
            selected_variant = next(iter(by_name.keys()), "A")

        selected_payload = by_name.get(selected_variant, {})
        final_subject = str(selected_payload.get("subject") or "")
        final_body = str(selected_payload.get("body") or "")

        dossier_sources: list[object] = []
        if isinstance(payload, dict):
            dossier = payload.get("dossier") or {}
            if isinstance(dossier, dict):
                dossier_sources = dossier.get("sources") or []

        generation_status = "OK" if passing else ("FAILED_COPY_GUARD" if by_name else str(payload.get("generation_status") or "OK"))
        error_code = "" if passing else ("FAILED_COPY_GUARD" if by_name else str(payload.get("error_code") or ""))
        instantly_payload = payload.get("instantly_draft") if isinstance(payload, dict) else None
        research_payload = payload.get("research_dossier") if isinstance(payload, dict) else None
        research_sources = (
            (research_payload or {}).get("research_sources")
            if isinstance(research_payload, dict)
            else payload.get("research_sources")
            if isinstance(payload, dict)
            else []
        )

        warning_parts: list[str] = []
        existing_warning = str(payload.get("generation_warning") or "") if isinstance(payload, dict) else ""
        if existing_warning:
            warning_parts.append(existing_warning)
        if passing and failed:
            warning_parts.append(f"Copy guard fallito per varianti: {', '.join(failed)}")
        generation_warning = "; ".join(part for part in warning_parts if part)[:240]

        risk_flags: set[str] = set()
        selected_flags = selected_payload.get("risk_flags") or []
        if isinstance(selected_flags, list):
            risk_flags.update(str(item) for item in selected_flags if str(item))
        if passing:
            risk_flags.discard("failed_copy_guard")
        if not dossier_sources:
            risk_flags.add("limited_sources")

        row.update(
            {
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
                "recommended_variant": payload.get("recommended_variant", "") if isinstance(payload, dict) else "",
                "final_subject": final_subject,
                "final_body": final_body,
                "selected_variant": selected_variant,
                "generation_status": generation_status,
                "generation_warning": generation_warning,
                "error_code": error_code,
                "SubjectLine": str((instantly_payload or {}).get("subject_line") or final_subject),
                "Personalization": str((instantly_payload or {}).get("personalization") or ""),
                "CTA": str((instantly_payload or {}).get("cta_line") or ""),
                "BodyTemplate": str((instantly_payload or {}).get("body_template") or ""),
                "Angle": str((research_payload or {}).get("personalization_angle") or ""),
                "Trigger": str((research_payload or {}).get("trigger_event") or ""),
                "ResearchSources": "; ".join(
                    str(item).strip() for item in research_sources if str(item).strip()
                )
                if isinstance(research_sources, list)
                else "",
                "Source1": (
                    str((research_payload or {}).get("citations", [""])[0] or "")
                    if isinstance((research_payload or {}).get("citations"), list)
                    else ""
                ),
                "evidence_summary": "; ".join((payload.get("dossier", {}) or {}).get("evidence", [])[:5])
                if isinstance(payload, dict)
                else "",
                "risk_flags": "; ".join(sorted(risk_flags)),
                "status": record.get("status") or "PENDING",
                "reviewer_notes": record.get("reviewer_notes") or "",
                "approved_variant": record.get("approved_variant") or "",
                "updated_at": str(record.get("updated_at") or ""),
            }
        )
        rows.append(row)

    columns = _merge_columns(raw_columns, approval_cols)
    target = Path(output_path)
    write_csv(target, rows, columns)
    return target


def export_instantly_campaign(store: PostgresStore, campaign_id: str, output_path: str) -> Path:
    records = store.list_campaign_records(campaign_id)
    rows: list[dict[str, object]] = []
    columns = [
        "Email",
        "FirstName",
        "LastName",
        "CompanyName",
        "SubjectLine",
        "Personalization",
        "CampaignId",
        "ParentSlug",
    ]

    for record in records:
        payload = record.get("payload_json") or {}
        if not isinstance(payload, dict):
            payload = {}
        instantly_payload = payload.get("instantly_draft") if isinstance(payload.get("instantly_draft"), dict) else {}
        raw_row = payload.get("raw_row") if isinstance(payload.get("raw_row"), dict) else {}
        email = str(record.get("contact_email") or raw_row.get("Email") or raw_row.get("email") or "").strip()
        if not email:
            continue
        contact_name = str(record.get("contact_name") or raw_row.get("Full Name") or "").strip()
        if not contact_name:
            contact_name = (
                f"{raw_row.get('First Name', '')} {raw_row.get('Last Name', '')}".strip()
                if isinstance(raw_row, dict)
                else ""
            )
        first_name, last_name = _split_contact_name(contact_name)
        subject_line = str(
            instantly_payload.get("subject_line")
            or payload.get("SubjectLine")
            or payload.get("final_subject")
            or ""
        ).strip()
        personalization = str(
            instantly_payload.get("personalization")
            or payload.get("Personalization")
            or ""
        ).strip()

        rows.append(
            {
                "Email": email,
                "FirstName": first_name,
                "LastName": last_name,
                "CompanyName": str(record.get("company_name") or "").strip(),
                "SubjectLine": subject_line,
                "Personalization": personalization,
                "CampaignId": campaign_id,
                "ParentSlug": str(record.get("parent_slug") or "").strip(),
            }
        )

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
    recipient_mode: str,
    max_concurrency: int,
    max_retries: int,
    backoff_base_seconds: float,
    output_schema: str,
    memory_snippets: list[str],
    research_client: ExaClient,
    research_sources: list[str],
) -> list[_RowOutcome]:
    valid_rows = [item for item in preflight.rows if item.is_valid]
    if not valid_rows:
        return []

    outcomes: list[_RowOutcome] = []
    workers = max(1, int(max_concurrency))
    total = len(valid_rows)
    done = 0
    llm_semaphore = threading.Semaphore(workers)
    
    def _worker_wrapper(**kwargs):
        with llm_semaphore:
            return _process_company_like_item(**kwargs)
            
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _worker_wrapper,
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
                recipient_mode=recipient_mode,
                max_retries=max_retries,
                    backoff_base_seconds=backoff_base_seconds,
                    output_schema=output_schema,
                    memory_snippets=memory_snippets,
                    row_index=item.row_index,
                    research_client=research_client,
                    research_sources=research_sources,
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
    recipient_mode: str,
    max_retries: int,
    backoff_base_seconds: float,
    output_schema: str,
    memory_snippets: list[str],
    research_client: ExaClient,
    research_sources: list[str],
    row_index: int = 0,
) -> _RowOutcome:
    company, contacts = build_company_and_contacts(canonical_rows)
    primary_contact = select_primary_contact(contacts)

    used_llm = False
    template_warning = ""
    enrichment_warning = ""
    snippets: list[str] = []
    enrichment_flags: list[str] = []
    nebula_payload: dict[str, object] = {}
    research_dossier = None
    instantly_draft = None

    checkpointer_builder = getattr(store, "build_langgraph_checkpointer", None)
    checkpointer = checkpointer_builder() if callable(checkpointer_builder) else None
    engine = CampaignAgentEngine(llm=llm, checkpointer=checkpointer)

    if research_client.configured and research_sources:
        try:
            used_llm = True
            research_bundle = research_client.collect_company_research(
                company=company,
                contact=primary_contact,
                research_sources=research_sources,
            )
            research_dossier = llm.generate_research_dossier(
                parent=parent,
                company=company,
                contact=primary_contact,
                research_bundle=research_bundle,
                llm_policy=llm_policy,
            )
            snippets.append(f"Research Summary: {research_dossier.company_summary}")
            if research_dossier.key_facts:
                snippets.append("Key Facts: " + " | ".join(research_dossier.key_facts))
            if research_dossier.recent_news:
                news_text = " | ".join(f"{n.title}: {n.snippet}" for n in research_dossier.recent_news)
                snippets.append("Recent News: " + news_text)
            dossier = _research_dossier_to_enrichment_dossier(research_dossier)
        except RuntimeError as exc:
            message = str(exc)
            return _RowOutcome(
                row_index=row_index,
                export_row=_error_row(
                    campaign_id=campaign_id,
                    parent_slug=parent_slug,
                    raw_row=raw_row,
                    error_code="FAILED_RESEARCH_PIPELINE",
                    warning_message=message,
                    output_schema=output_schema,
                ),
                result=None,
                extra_payload={
                    "used_llm": True,
                    "research_mode": "exa_openrouter",
                    "research_sources": research_sources,
                },
                warning=True,
                failed=True,
                fatal_error=(llm_policy == "strict"),
                error_message=message,
            )
    else:
        if not company.website:
            template_warning = "Website mancante: usato seed template senza personalizzazione web."
            enrichment_flags.append("template_only_no_website")
            dossier = _minimal_dossier(company_name=company.company_name)
            nebula = run_nebula_enrichment_machine(
                company=company,
                contact=primary_contact,
                dossier=dossier,
            )
            nebula_payload = nebula.to_dict()
        else:
            used_llm = True
            if effective_enrichment_mode == "minimal":
                dossier = _minimal_dossier(company_name=company.company_name)
                discovered_website = company.website
            else:
                extra_pages = 0 if recipient_mode == "row" else 2
                snapshot_timeout_ms = 18000 if recipient_mode == "row" else 45000
                dossier, discovered_website = build_enrichment_dossier_sync(
                    company=company,
                    contact=primary_contact,
                    headless=headless,
                    max_extra_pages=extra_pages,
                    snapshot_timeout_ms=snapshot_timeout_ms,
                )
            if discovered_website and not company.website:
                company.website = discovered_website

            if effective_enrichment_mode != "minimal":
                evidence_items = dossier.evidence or []
                if any("Sito non analizzabile" in str(item) for item in evidence_items):
                    enrichment_flags.append("enrichment_site_unreachable")
                    enrichment_warning = "Sito non analizzabile: personalizzazione web limitata."
                elif len((dossier.site_summary or "").strip()) < 120:
                    enrichment_flags.append("enrichment_site_low_content")
                    enrichment_warning = "Contenuti sito non estratti: personalizzazione web limitata."

            nebula = run_nebula_enrichment_machine(
                company=company,
                contact=primary_contact,
                dossier=dossier,
            )
            nebula_payload = nebula.to_dict()
            snippets.extend(nebula.to_prompt_snippets(limit=10))
            snippets.extend(memory_snippets)
            if nebula.depth == "low":
                enrichment_flags.append("nebula_low_signal")
                if not enrichment_warning:
                    enrichment_warning = "Arricchimento limitato: pochi segnali aziendali affidabili."

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
                    rag_snippets = [str(item.get("content") or "") for item in search_results if item.get("content")]
                    snippets.extend(rag_snippets)

            snippets = _dedupe_snippets(snippets, limit=14)

    supports_agent_chat = callable(getattr(llm, "_call_chat_json", None)) or callable(
        getattr(llm, "_call_chat_json_targets", None)
    )
    supports_legacy_variants = callable(getattr(llm, "generate_campaign_variants", None))
    supports_instantly_draft = callable(getattr(llm, "generate_instantly_draft", None))

    if (not supports_agent_chat) and (supports_legacy_variants or supports_instantly_draft):
        return _process_company_like_item_legacy(
            campaign_id=campaign_id,
            parent_slug=parent_slug,
            parent=parent,
            company=company,
            primary_contact=primary_contact,
            dossier=dossier,
            raw_row=raw_row,
            llm=llm,
            variant_mode=variant_mode,
            llm_policy=llm_policy,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            output_schema=output_schema,
            used_llm=used_llm,
            nebula_payload=nebula_payload,
            enrichment_flags=enrichment_flags,
            template_warning=template_warning,
            enrichment_warning=enrichment_warning,
            snippets=snippets,
            research_dossier=research_dossier,
            research_sources=research_sources,
            row_index=row_index,
        )

    if research_dossier is not None and supports_instantly_draft:
        try:
            instantly_draft = llm.generate_instantly_draft(
                parent=parent,
                company=company,
                contact=primary_contact,
                research_dossier=research_dossier,
                llm_policy=llm_policy,
            )
        except Exception:
            instantly_draft = None

    try:
        sequence = engine.generate_sequence(
            parent=parent,
            company=company,
            contact=primary_contact,
            dossier=dossier,
            marketing_snippets=snippets,
            llm_policy=llm_policy,
        )
    except Exception as exc:
        message = str(exc)
        if llm_policy == "strict" and ("LLM fatal error" in message or "LLM unavailable" in message):
            return _RowOutcome(
                row_index=row_index,
                export_row={},
                result=None,
                extra_payload={
                    "used_llm": used_llm,
                    "nebula": nebula_payload,
                },
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
            extra_payload={
                "used_llm": used_llm,
                "nebula": nebula_payload,
            },
            warning=True,
            failed=True,
            fatal_error=False,
            error_message=message,
        )

    risk_flags = sorted(
        set(sequence.global_risk_flags + [flag for step in sequence.steps for flag in step.risk_flags] + enrichment_flags)
    )

    result = CampaignCompanyResult(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        company=company,
        contact=primary_contact,
        dossier=dossier,
        variants=[],
        recommended_variant="",
        approval=ApprovalRecord(status="PENDING", updated_at=utc_now_iso()),
        sequence_result=sequence,
        research_dossier=research_dossier,
        instantly_draft=instantly_draft,
        risk_flags=risk_flags,
    )

    selected_step = next((item for item in sequence.steps if item.step_id.upper() == "E1"), None)
    final_subject = selected_step.subject if selected_step else ""
    final_body = selected_step.body if selected_step else ""
    warning_parts = [part for part in [template_warning, enrichment_warning] if part]
    if risk_flags:
        warning_parts.append("; ".join(risk_flags))
    generation_warning = "; ".join(part for part in warning_parts if part)[:240]

    export_row = _company_result_to_row(
        result=result,
        raw_row=raw_row,
        selected_variant="A",
        final_subject=final_subject,
        final_body=final_body,
        generation_status="OK",
        generation_warning=generation_warning,
        error_code="",
        output_schema=output_schema,
    )
    export_row["sequence_steps"] = len(sequence.steps)
    export_row["attack_angle"] = sequence.attack_angle

    extra_payload = {
        "final_subject": final_subject,
        "final_body": final_body,
        "generation_status": "OK",
        "generation_warning": generation_warning,
        "error_code": "",
        "used_llm": used_llm,
        "raw_row": raw_row,
        "nebula": nebula_payload,
        "research_sources": list(research_sources),
    }
    if research_dossier is not None:
        extra_payload["research_dossier"] = research_dossier.model_dump()
    if instantly_draft is not None:
        extra_payload["instantly_draft"] = instantly_draft.model_dump()

    return _RowOutcome(
        row_index=row_index,
        export_row=export_row,
        result=result,
        extra_payload=extra_payload,
        warning=bool(risk_flags),
        failed=False,
        fatal_error=False,
        error_message=None,
    )


def _process_company_like_item_legacy(
    *,
    campaign_id: str,
    parent_slug: str,
    parent,
    company,
    primary_contact,
    dossier: EnrichmentDossier,
    raw_row: dict[str, str],
    llm: LLMGateway,
    variant_mode: str,
    llm_policy: str,
    max_retries: int,
    backoff_base_seconds: float,
    output_schema: str,
    used_llm: bool,
    nebula_payload: dict[str, object],
    enrichment_flags: list[str],
    template_warning: str,
    enrichment_warning: str,
    snippets: list[str],
    research_dossier,
    research_sources: list[str],
    row_index: int,
) -> _RowOutcome:
    requested_variants = ["A", "B"] if (variant_mode or "ab").lower() == "ab" else ["A", "B", "C"]
    variants: list[DraftEmailVariant] = []
    recommended_variant = requested_variants[0]
    global_flags: list[str] = []

    generate_variants = getattr(llm, "generate_campaign_variants", None)
    if callable(generate_variants):
        try:
            variants, recommended_variant, global_flags = generate_variants(
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
            used_llm = True
        except Exception as exc:
            message = str(exc)
            if llm_policy == "strict":
                return _RowOutcome(
                    row_index=row_index,
                    export_row={},
                    result=None,
                    extra_payload={"used_llm": used_llm, "nebula": nebula_payload},
                    warning=True,
                    failed=True,
                    fatal_error=True,
                    error_message=message,
                )

    if not variants:
        for idx, name in enumerate(requested_variants):
            suffix = ""
            if idx == 1:
                suffix = " - follow-up"
            elif idx == 2:
                suffix = " - ultimo tentativo"
            variants.append(
                DraftEmailVariant(
                    variant=name,
                    subject=format_email_subject(_template_only_subject(company=company, contact=primary_contact) + suffix),
                    body=format_email_body(render_seed_template(parent, company, primary_contact)),
                    cta=parent.cta_policy,
                    risk_flags=[],
                    confidence=0.0,
                )
            )
        recommended_variant = requested_variants[0]

    instantly_draft = None
    generate_instantly = getattr(llm, "generate_instantly_draft", None)
    if research_dossier is not None and callable(generate_instantly):
        try:
            instantly_draft = generate_instantly(
                parent=parent,
                company=company,
                contact=primary_contact,
                research_dossier=research_dossier,
                llm_policy=llm_policy,
            )
        except Exception:
            instantly_draft = None

    by_name = _variants_by_name(variants)
    recommended = str(recommended_variant or requested_variants[0]).upper()
    if recommended not in by_name:
        recommended = next(iter(by_name.keys()), requested_variants[0])

    passing = [
        name
        for name in ["A", "B", "C"]
        if name in by_name and "failed_copy_guard" not in list(by_name[name].get("risk_flags") or [])
    ]
    failed = [name for name in ["A", "B", "C"] if name in by_name and name not in passing]

    selected_variant = recommended
    if passing and selected_variant not in passing:
        selected_variant = passing[0]
    if selected_variant not in by_name:
        selected_variant = next(iter(by_name.keys()), requested_variants[0])

    selected_payload = by_name.get(selected_variant, {})
    final_subject = str(selected_payload.get("subject") or _template_only_subject(company=company, contact=primary_contact))
    final_body = str(selected_payload.get("body") or render_seed_template(parent, company, primary_contact))

    generation_status = "OK" if passing else ("FAILED_COPY_GUARD" if by_name else "ERROR")
    error_code = "" if generation_status == "OK" else ("FAILED_COPY_GUARD" if by_name else "FAILED_LLM_RETRY_EXHAUSTED")

    warning_parts: list[str] = []
    for part in [template_warning, enrichment_warning]:
        value = str(part or "").strip()
        if value:
            warning_parts.append(value)
    if passing and failed:
        warning_parts.append(f"Copy guard fallito per varianti: {', '.join(failed)}")
    elif not passing and failed:
        warning_parts.append(f"Tutte le varianti hanno fallito il copy guard: {', '.join(failed)}")
    generation_warning = "; ".join(warning_parts)[:240]

    risk_flags = set(str(item) for item in global_flags if str(item))
    risk_flags.update(enrichment_flags)
    for item in list(selected_payload.get("risk_flags") or []):
        text = str(item).strip()
        if text:
            risk_flags.add(text)
    if passing:
        risk_flags.discard("failed_copy_guard")

    result = CampaignCompanyResult(
        campaign_id=campaign_id,
        parent_slug=parent_slug,
        company=company,
        contact=primary_contact,
        dossier=dossier,
        variants=variants,
        recommended_variant=recommended,
        approval=ApprovalRecord(status="PENDING", updated_at=utc_now_iso()),
        sequence_result=None,
        research_dossier=research_dossier,
        instantly_draft=instantly_draft,
        risk_flags=sorted(risk_flags),
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
        "final_subject": final_subject,
        "final_body": final_body,
        "generation_status": generation_status,
        "generation_warning": generation_warning,
        "error_code": error_code,
        "used_llm": used_llm,
        "raw_row": raw_row,
        "nebula": nebula_payload,
        "selected_variant": selected_variant,
        "recommended_variant": recommended,
        "research_sources": list(research_sources),
    }
    if research_dossier is not None:
        extra_payload["research_dossier"] = research_dossier.model_dump()
    if instantly_draft is not None:
        extra_payload["instantly_draft"] = instantly_draft.model_dump()

    return _RowOutcome(
        row_index=row_index,
        export_row=export_row,
        result=result,
        extra_payload=extra_payload,
        warning=bool(generation_warning or risk_flags),
        failed=(generation_status != "OK"),
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


def _row_has_valid_website(row: dict[str, str]) -> bool:
    website = (row.get("Company Website Full") or "").strip()
    if not website:
        return False
    parsed = urlparse(website)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    return True


def _estimate_llm_items_planned(*, preflight, recipient_mode: str) -> int:
    mode = (recipient_mode or "row").lower()
    if mode == "row":
        return sum(1 for item in preflight.rows if item.is_valid and _row_has_valid_website(item.row))

    valid_rows = [item.row for item in preflight.rows if item.is_valid]
    groups = group_rows_by_company(valid_rows)
    planned = 0
    for company_rows in groups.values():
        if any(_row_has_valid_website(row) for row in company_rows):
            planned += 1
    return planned


def _estimate_cost_eur(llm_items: int) -> float:
    estimated = llm_items * 0.05
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


def _load_email_memory_snippets(*, store: PostgresStore, parent_slug: str, limit: int = 12) -> list[str]:
    list_memories = getattr(store, "list_agent_memories", None)
    if not callable(list_memories):
        return []

    try:
        memories = list_memories(parent_slug=parent_slug, kind="email_playbook", limit=3)
    except Exception:
        return []

    snippets: list[str] = []
    for memory in memories:
        payload = memory.get("memory_json")
        if not isinstance(payload, dict):
            continue
        top_subjects = payload.get("top_subjects") or []
        for item in top_subjects[:3]:
            text = str(item).strip()
            if text:
                snippets.append(f"[MemorySubject] {text}")
        avoid_flags = payload.get("avoid_flags") or []
        for item in avoid_flags[:5]:
            text = str(item).strip()
            if text:
                snippets.append(f"[MemoryAvoidFlag] {text}")
        preferred_signals = payload.get("preferred_signal_terms") or []
        for item in preferred_signals[:4]:
            text = str(item).strip()
            if text:
                snippets.append(f"[MemorySignal] {text}")

    return _dedupe_snippets(snippets, limit=limit)


def _persist_email_memory_snapshot(
    *,
    store: PostgresStore,
    parent_slug: str,
    summary_rows: list[dict[str, object]],
    rows_total: int,
    rows_generated_ok: int,
    rows_failed: int,
) -> None:
    insert_memory = getattr(store, "insert_agent_memory", None)
    if not callable(insert_memory):
        return

    rows_ok = [row for row in summary_rows if str(row.get("generation_status") or "") == "OK"]
    top_subjects: list[str] = []
    for row in rows_ok:
        subject = str(row.get("final_subject") or row.get("variant_a_subject") or "").strip()
        if subject and subject not in top_subjects:
            top_subjects.append(subject)
        if len(top_subjects) >= 8:
            break

    flag_counter: Counter[str] = Counter()
    signal_counter: Counter[str] = Counter()
    for row in summary_rows:
        raw_flags = str(row.get("risk_flags") or "")
        for flag in [item.strip() for item in raw_flags.split(";") if item.strip()]:
            flag_counter[flag] += 1

        evidence = str(row.get("evidence_summary") or "").lower()
        for token in ("industry", "homepage title", "location", "linkedin", "news", "employee count"):
            if token in evidence:
                signal_counter[token] += 1

    avoid_flags = [flag for flag, _ in flag_counter.most_common(8) if flag not in {"limited_sources"}]
    preferred_signals = [token for token, _ in signal_counter.most_common(6)]

    quality_score = round(rows_generated_ok / max(rows_total, 1), 4)
    snapshot = {
        "rows_total": rows_total,
        "rows_generated_ok": rows_generated_ok,
        "rows_failed": rows_failed,
        "top_subjects": top_subjects,
        "avoid_flags": avoid_flags,
        "preferred_signal_terms": preferred_signals,
        "note": "Memoria auto-generata da run campagna per migliorare prompt successivi.",
        "created_at": utc_now_iso(),
    }
    try:
        insert_memory(parent_slug=parent_slug, kind="email_playbook", memory=snapshot, score=quality_score)
    except Exception:
        # Memory write must never block campaign completion.
        return


def _dedupe_snippets(items: list[str], *, limit: int = 14) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = " ".join(str(item).split()).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= max(1, limit):
            break
    return out


def _minimal_dossier(*, company_name: str) -> EnrichmentDossier:
    return EnrichmentDossier(
        site_summary=f"Dossier minimale generato da CSV per {company_name}.",
        pain_hypotheses=["allineamento priorita commerciali e execution operativa"],
        opportunity_hypotheses=["definire quick win con impatto commerciale tracciabile"],
        evidence=["Fonte primaria: CSV lead"],
        sources=["csv://lead-row"],
    )


def _research_dossier_to_enrichment_dossier(research_dossier) -> EnrichmentDossier:
    return EnrichmentDossier(
        site_summary=research_dossier.company_summary or f"Dossier sintetico per {research_dossier.company_name}.",
        news_items=[
            SearchHit(title=item.title, url=item.url, snippet=item.snippet)
            for item in research_dossier.recent_news
        ],
        linkedin_public_summary="",
        pain_hypotheses=[research_dossier.pain_hypothesis] if research_dossier.pain_hypothesis else [],
        opportunity_hypotheses=[research_dossier.personalization_angle] if research_dossier.personalization_angle else [],
        evidence=list(research_dossier.key_facts or []),
        sources=list(research_dossier.citations or []),
    )


def _template_only_subject(*, company, contact) -> str:
    first_name = ""
    if contact and getattr(contact, "full_name", ""):
        first_name = str(contact.full_name).split()[0].strip()
    if first_name:
        subject = f"{first_name}, contributi a fondo perduto per {company.company_name}"
    else:
        subject = f"Contributi a fondo perduto per {company.company_name}"
    subject = " ".join(subject.split())
    return subject[:70].rstrip()


def _build_retrieval_query(*, company, dossier) -> str:
    hints = [
        company.company_name,
        company.industry or "",
        company.keywords or "",
        " ".join(dossier.pain_hypotheses[:2]),
        " ".join(dossier.opportunity_hypotheses[:2]),
    ]
    return " | ".join(part for part in hints if part)


def _pick_raw_row_for_company(preflight_rows, company_row: dict[str, str]) -> dict[str, str] | None:
    company_name = (company_row.get("Company Name") or "").strip().lower()
    for item in preflight_rows:
        if not item.is_valid:
            continue
        if (item.row.get("Company Name") or "").strip().lower() == company_name:
            return item.raw_row
    print(f"[warning] No raw row found for company row: {company_row}")
    return None


def _split_contact_name(value: str) -> tuple[str, str]:
    parts = [item for item in str(value or "").split() if item]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


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
            "SubjectLine": "",
            "Personalization": "",
            "CTA": "",
            "BodyTemplate": "",
            "Angle": "",
            "Trigger": "",
            "ResearchSources": "",
            "Source1": "",
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
            "SubjectLine": "",
            "Personalization": "",
            "CTA": "",
            "BodyTemplate": "",
            "Angle": "",
            "Trigger": "",
            "ResearchSources": "",
            "Source1": "",
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
    instantly_draft = result.instantly_draft
    research_dossier = result.research_dossier
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
            "SubjectLine": instantly_draft.subject_line if instantly_draft else final_subject,
            "Personalization": instantly_draft.personalization if instantly_draft else "",
            "CTA": instantly_draft.cta_line if instantly_draft else "",
            "BodyTemplate": instantly_draft.body_template if instantly_draft else "",
            "Angle": research_dossier.personalization_angle if research_dossier else "",
            "Trigger": research_dossier.trigger_event if research_dossier else "",
            "ResearchSources": "; ".join(research_dossier.research_sources) if research_dossier else "",
            "Source1": (research_dossier.citations[0] if research_dossier and research_dossier.citations else ""),
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
        out[variant.variant.upper()] = variant.model_dump()
    return out
