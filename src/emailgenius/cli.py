from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict
from pathlib import Path

from .campaign import campaign_status, export_campaign, run_campaign
from .config import AppConfig
from .knowledge import ingest_knowledge_file
from .llm import LLMGateway
from .pipeline import analyze_company_sync, discover_and_analyze_company_sync, result_to_dict
from .profiles import load_parent_profile
from .sheets import publish_campaign_to_sheets
from .storage import PostgresStore
from .utils import slugify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emailgenius")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a company website")
    analyze_parser.add_argument("url", help="Company website URL")
    analyze_parser.add_argument("--company", default="Azienda", help="Company display name")
    analyze_parser.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    analyze_parser.add_argument("--show-email", action="store_true", help="Print outreach draft")
    analyze_parser.add_argument("--out", help="Output JSON path")

    discover_parser = subparsers.add_parser(
        "discover",
        help="Discover website + news from company name/city, then analyze",
    )
    discover_parser.add_argument("--company", required=True, help="Company display name")
    discover_parser.add_argument("--city", help="City used for discovery query")
    discover_parser.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    discover_parser.add_argument("--show-email", action="store_true", help="Print outreach draft")
    discover_parser.add_argument("--show-news", action="store_true", help="Print top discovered news links")
    discover_parser.add_argument("--news-results", type=int, default=6, help="Number of news links to include")
    discover_parser.add_argument("--site-results", type=int, default=8, help="Number of website candidates")
    discover_parser.add_argument("--out", help="Output JSON path")

    parent_parser = subparsers.add_parser("parent", help="Manage parent company profiles")
    parent_sub = parent_parser.add_subparsers(dest="parent_command", required=True)

    parent_register = parent_sub.add_parser("register", help="Register or update parent profile")
    parent_register.add_argument("--slug", required=True, help="Parent slug")
    parent_register.add_argument("--profile", required=True, help="YAML profile path")
    parent_register.add_argument("--set-active", action="store_true", help="Set as active parent")

    parent_use = parent_sub.add_parser("use", help="Set active parent profile")
    parent_use.add_argument("--slug", required=True, help="Parent slug")

    parent_sub.add_parser("list", help="List registered parent profiles")

    knowledge_parser = subparsers.add_parser("knowledge", help="Manage RAG knowledge")
    knowledge_sub = knowledge_parser.add_subparsers(dest="knowledge_command", required=True)

    knowledge_ingest = knowledge_sub.add_parser("ingest", help="Ingest knowledge file")
    knowledge_ingest.add_argument("--slug", required=True, help="Parent slug")
    knowledge_ingest.add_argument("--file", required=True, help="Path to PDF/DOCX/MD")
    knowledge_ingest.add_argument("--kind", default="marketing", help="Knowledge kind")

    knowledge_list = knowledge_sub.add_parser("list", help="List ingested knowledge docs")
    knowledge_list.add_argument("--slug", required=True, help="Parent slug")

    campaign_parser = subparsers.add_parser("campaign", help="Run and manage campaigns")
    campaign_sub = campaign_parser.add_subparsers(dest="campaign_command", required=True)

    campaign_run = campaign_sub.add_parser("run", help="Run campaign from leads CSV")
    campaign_run.add_argument("--slug", required=True, help="Parent slug")
    campaign_run.add_argument("--leads", help="Leads CSV path (required in --io-mode local)")
    campaign_run.add_argument(
        "--io-mode",
        default="local",
        choices=["local", "drive"],
        help="I/O mode: local file system or Drive-native workspace",
    )
    campaign_run.add_argument(
        "--workspace-folder-id",
        help="Google Drive workspace folder id (required in --io-mode drive if not set in env)",
    )
    campaign_run.add_argument("--sheet-id", help="Google Sheet id for approval queue")
    campaign_run.add_argument(
        "--sheet-title",
        help="Create a new Google Sheet with this title (requires --gsheets-auth oauth or GOOGLE_SERVICE_ACCOUNT_JSON)",
    )
    campaign_run.add_argument(
        "--sheet-share-with",
        help="Share the Google Sheet with this email as writer (service-account runs typically need this)",
    )
    campaign_run.add_argument(
        "--drive-folder-id",
        help="Google Drive folder id (or folder URL) where the sheet should be moved",
    )
    campaign_run.add_argument(
        "--gsheets-auth",
        default="auto",
        choices=["auto", "service_account", "oauth"],
        help="Google Sheets auth mode: auto uses service account if configured",
    )
    campaign_run.add_argument("--out-dir", default="reports/campaigns", help="Output directory")
    campaign_run.add_argument("--stages", default="all", help="Pipeline stages (default: all)")
    campaign_run.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    campaign_run.add_argument(
        "--recipient-mode",
        default="company",
        choices=["company", "row"],
        help="Recipient granularity: company (default) or row",
    )
    campaign_run.add_argument(
        "--variant-mode",
        default="ab",
        choices=["ab", "abc"],
        help="Generated variant set",
    )
    campaign_run.add_argument(
        "--output-schema",
        default="ab",
        choices=["ab", "abc"],
        help="Output schema used for CSV/sheet",
    )
    campaign_run.add_argument(
        "--llm-policy",
        default="strict",
        choices=["strict", "fallback"],
        help="LLM error policy",
    )
    campaign_run.add_argument(
        "--enrichment-mode",
        default="auto",
        choices=["auto", "minimal", "hybrid", "web"],
        help="Enrichment intensity",
    )
    campaign_run.add_argument("--max-concurrency", type=int, default=5, help="Max concurrent workers")
    campaign_run.add_argument("--max-retries", type=int, default=3, help="Retries for transient LLM errors")
    campaign_run.add_argument("--backoff-base-seconds", type=float, default=1.0, help="Exponential backoff base")
    campaign_run.add_argument("--cost-cap-eur", type=float, default=50.0, help="Pre-run cost cap")
    campaign_run.add_argument(
        "--force-cost-override",
        action="store_true",
        help="Force run even when estimated cost exceeds cap",
    )

    campaign_status_parser = campaign_sub.add_parser("status", help="Campaign status")
    campaign_status_parser.add_argument("--campaign-id", required=True, help="Campaign id")

    campaign_export_parser = campaign_sub.add_parser("export", help="Export campaign rows")
    campaign_export_parser.add_argument("--campaign-id", required=True, help="Campaign id")
    campaign_export_parser.add_argument("--format", default="csv", choices=["csv"], help="Export format")
    campaign_export_parser.add_argument("--out", required=True, help="Output path")
    campaign_export_parser.add_argument(
        "--output-schema",
        default="auto",
        choices=["auto", "ab", "abc"],
        help="Export schema",
    )

    campaign_publish_sheet = campaign_sub.add_parser(
        "publish-sheet",
        help="Publish an existing campaign CSV to Google Sheets without re-running enrichment/generation",
    )
    campaign_publish_sheet.add_argument("--csv", required=True, help="Path to an existing campaign CSV")
    campaign_publish_sheet.add_argument("--sheet-id", help="Target Google Sheet id (optional)")
    campaign_publish_sheet.add_argument("--sheet-title", help="Create a new Google Sheet with this title")
    campaign_publish_sheet.add_argument(
        "--sheet-share-with",
        help="Share the Google Sheet with this email as writer",
    )
    campaign_publish_sheet.add_argument(
        "--drive-folder-id",
        help="Google Drive folder id (or folder URL) where the sheet should be moved",
    )
    campaign_publish_sheet.add_argument(
        "--gsheets-auth",
        default="auto",
        choices=["auto", "service_account", "oauth"],
        help="Google Sheets auth mode",
    )
    campaign_publish_sheet.add_argument(
        "--output-schema",
        default="ab",
        choices=["ab", "abc"],
        help="Approval schema (Drafts tab columns)",
    )

    workspace_parser = subparsers.add_parser("workspace", help="Drive-native workspace orchestrator")
    workspace_sub = workspace_parser.add_subparsers(dest="workspace_command", required=True)

    workspace_sync = workspace_sub.add_parser("sync-once", help="Run one Drive-native sync/process cycle")
    workspace_sync.add_argument("--slug", help="Fallback parent slug when row parent_slug is missing")
    workspace_sync.add_argument("--workspace-folder-id", help="Google Drive workspace folder id")
    workspace_sync.add_argument("--out-dir", default="reports/campaigns", help="Output directory")
    workspace_sync.add_argument(
        "--gsheets-auth",
        default="auto",
        choices=["auto", "service_account", "oauth"],
        help="Google auth mode",
    )
    workspace_sync.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    workspace_sync.add_argument(
        "--llm-policy",
        default="strict",
        choices=["strict", "fallback"],
        help="LLM error policy",
    )

    workspace_daemon = workspace_sub.add_parser("daemon", help="Continuously process Drive workspace")
    workspace_daemon.add_argument("--slug", help="Fallback parent slug when row parent_slug is missing")
    workspace_daemon.add_argument("--workspace-folder-id", help="Google Drive workspace folder id")
    workspace_daemon.add_argument("--out-dir", default="reports/campaigns", help="Output directory")
    workspace_daemon.add_argument(
        "--gsheets-auth",
        default="auto",
        choices=["auto", "service_account", "oauth"],
        help="Google auth mode",
    )
    workspace_daemon.add_argument("--poll-interval-seconds", type=int, default=60, help="Loop polling interval")
    workspace_daemon.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    workspace_daemon.add_argument(
        "--llm-policy",
        default="strict",
        choices=["strict", "fallback"],
        help="LLM error policy",
    )

    app_parser = subparsers.add_parser("app", help="Run local web app")
    app_sub = app_parser.add_subparsers(dest="app_command", required=True)
    app_serve = app_sub.add_parser("serve", help="Serve EmailGenius web app locally")
    app_serve.add_argument("--host", default="127.0.0.1", help="Bind host")
    app_serve.add_argument("--port", type=int, default=8080, help="Bind port")
    app_serve.add_argument("--reload", action="store_true", help="Enable auto-reload")

    return parser


def _persist_json(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _store(config: AppConfig) -> PostgresStore:
    try:
        store = PostgresStore(config.database_url)
        store.migrate()
        return store
    except Exception as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "Database unavailable. Configure EMAILGENIUS_DATABASE_URL and ensure PostgreSQL+pgvector is running."
        ) from exc


def _llm(config: AppConfig) -> LLMGateway:
    return LLMGateway(
        api_key=config.openai_api_key,
        api_base_url=config.openai_base_url,
        chat_model=config.openai_chat_model,
        embedding_model=config.openai_embedding_model,
        fallback_api_key=config.openai_fallback_api_key,
        fallback_api_base_url=config.openai_fallback_base_url,
        fallback_chat_model=config.openai_fallback_chat_model,
        research_model=config.research_model,
        writer_model=config.writer_model,
        writer_fallback_model=config.writer_fallback_model,
    )


def _resolve_workspace_slug(store: PostgresStore, explicit_slug: str | None) -> str:
    if explicit_slug:
        return explicit_slug
    active = store.get_active_parent_slug()
    if active:
        return active
    profiles = store.list_parent_profiles()
    if len(profiles) == 1:
        return profiles[0].slug
    raise ValueError("Parent slug not resolved. Pass --slug or set an active parent profile.")


def _run_workspace_sync_once(
    *,
    config: AppConfig,
    store: PostgresStore,
    llm: LLMGateway,
    slug: str | None,
    workspace_folder_id: str | None,
    out_dir: str,
    gsheets_auth: str,
    headless: bool,
    llm_policy: str,
) -> int:
    resolved_slug = _resolve_workspace_slug(store, slug)
    summary, export_path, _ = run_campaign(
        config=config,
        store=store,
        llm=llm,
        parent_slug=resolved_slug,
        leads_csv_path=None,
        out_dir=out_dir,
        sheet_id=None,
        gsheets_auth=gsheets_auth,
        stages="all",
        headless=headless,
        recipient_mode="row",
        variant_mode="ab",
        output_schema="ab",
        llm_policy=llm_policy,
        enrichment_mode="auto",
        max_concurrency=1,
        max_retries=3,
        backoff_base_seconds=1.0,
        cost_cap_eur=999999.0,
        force_cost_override=True,
        io_mode="drive",
        workspace_folder_id=workspace_folder_id or config.workspace_folder_id,
    )
    print(f"[workspace] campaign={summary.campaign_id} rows_ok={summary.rows_generated_ok} rows_failed={summary.rows_failed}")
    print(f"[workspace] export={export_path}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "analyze":
        result = analyze_company_sync(
            url=args.url,
            company_name=args.company,
            headless=not args.headful,
        )
        payload = result_to_dict(result)

        output_path = Path(args.out) if args.out else Path("reports") / f"{slugify(args.company)}.json"
        _persist_json(payload, output_path)

        print(f"Saved analysis to: {output_path}")
        print(
            "Eligibility: "
            f"{result.eligibility.eligible} | rate={result.eligibility.estimated_credit_rate} "
            f"| confidence={result.eligibility.confidence}"
        )
        if args.show_email:
            print("\n--- Outreach Draft ---\n")
            print(result.outreach_email)
        return 0

    if args.command == "discover":
        try:
            result = discover_and_analyze_company_sync(
                company_name=args.company,
                city=args.city,
                headless=not args.headful,
                site_max_results=args.site_results,
                news_max_results=args.news_results,
            )
        except RuntimeError as exc:
            print(f"Discovery failed: {exc}")
            return 1

        payload = result_to_dict(result)
        city_part = f"-{slugify(args.city)}" if args.city else ""
        output_path = Path(args.out) if args.out else Path("reports") / f"{slugify(args.company)}{city_part}.json"
        _persist_json(payload, output_path)

        print(f"Saved analysis to: {output_path}")
        if result.discovery and result.discovery.selected_site:
            print(f"Selected website: {result.discovery.selected_site.url}")
            print(f"Site query: {result.discovery.site_query}")
            print(f"News query: {result.discovery.news_query}")
        print(
            "Eligibility: "
            f"{result.eligibility.eligible} | rate={result.eligibility.estimated_credit_rate} "
            f"| confidence={result.eligibility.confidence}"
        )
        if args.show_news and result.discovery:
            print("\n--- News Links ---")
            for index, item in enumerate(result.discovery.news_results, start=1):
                print(f"{index}. {item.title} -> {item.url}")

        if args.show_email:
            print("\n--- Outreach Draft ---\n")
            print(result.outreach_email)
        return 0

    config = AppConfig.from_env()

    if args.command == "parent":
        try:
            store = _store(config)
        except RuntimeError as exc:
            print(str(exc))
            return 1
        if args.parent_command == "register":
            profile = load_parent_profile(args.profile, slug_override=args.slug)
            store.upsert_parent_profile(profile, set_active=args.set_active)
            print(f"Parent profile upserted: {profile.slug}")
            if args.set_active:
                print(f"Active parent set to: {profile.slug}")
            return 0

        if args.parent_command == "use":
            store.set_active_parent(args.slug)
            print(f"Active parent set to: {args.slug}")
            return 0

        if args.parent_command == "list":
            active_slug = store.get_active_parent_slug()
            for profile in store.list_parent_profiles():
                marker = "*" if profile.slug == active_slug else " "
                print(f"{marker} {profile.slug} -> {profile.company_name}")
            return 0

    if args.command == "knowledge":
        try:
            store = _store(config)
        except RuntimeError as exc:
            print(str(exc))
            return 1
        llm = _llm(config)

        if args.knowledge_command == "ingest":
            profile = store.get_parent_profile(args.slug)
            if profile is None:
                print(f"Parent slug not found: {args.slug}")
                return 1

            result = ingest_knowledge_file(
                store=store,
                llm=llm,
                parent_slug=args.slug,
                file_path=args.file,
                kind=args.kind,
            )
            print(
                f"Knowledge ingested for {result.parent_slug}: {result.source_path} | "
                f"chunks={result.chunks_total} | embeddings={result.embeddings_used}"
            )
            return 0

        if args.knowledge_command == "list":
            docs = store.list_knowledge_documents(args.slug)
            if not docs:
                print("No documents found.")
                return 0
            for item in docs:
                print(f"{item['id']} | {item['kind']} | {item['source_path']} | {item['created_at']}")
            return 0

    if args.command == "workspace":
        try:
            store = _store(config)
        except RuntimeError as exc:
            print(str(exc))
            return 1
        llm = _llm(config)

        if args.workspace_command == "sync-once":
            try:
                return _run_workspace_sync_once(
                    config=config,
                    store=store,
                    llm=llm,
                    slug=args.slug,
                    workspace_folder_id=args.workspace_folder_id,
                    out_dir=args.out_dir,
                    gsheets_auth=args.gsheets_auth,
                    headless=not args.headful,
                    llm_policy=args.llm_policy,
                )
            except Exception as exc:
                print(f"[workspace] sync failed: {exc}")
                return 1

        if args.workspace_command == "daemon":
            interval = max(5, int(args.poll_interval_seconds))
            print(f"[workspace] daemon started | interval={interval}s")
            while True:
                started_at = time.time()
                try:
                    _run_workspace_sync_once(
                        config=config,
                        store=store,
                        llm=llm,
                        slug=args.slug,
                        workspace_folder_id=args.workspace_folder_id,
                        out_dir=args.out_dir,
                        gsheets_auth=args.gsheets_auth,
                        headless=not args.headful,
                        llm_policy=args.llm_policy,
                    )
                except Exception as exc:
                    print(f"[workspace] cycle failed: {exc}")
                elapsed = time.time() - started_at
                sleep_seconds = max(1, interval - int(elapsed))
                time.sleep(sleep_seconds)

    if args.command == "app":
        if args.app_command == "serve":
            try:
                import uvicorn
            except Exception as exc:  # pragma: no cover - dependency/runtime dependent
                print(f"Web app dependencies unavailable: {exc}")
                return 1
            uvicorn.run(
                "emailgenius.webapp:create_app",
                factory=True,
                host=args.host,
                port=max(1, int(args.port)),
                reload=bool(args.reload),
            )
            return 0

    if args.command == "campaign":
        if args.campaign_command == "run":
            try:
                store = _store(config)
            except RuntimeError as exc:
                print(str(exc))
                return 1
            llm = _llm(config)
            if args.io_mode == "local" and not args.leads:
                print("--leads is required when --io-mode local")
                return 1
            summary, export_path, _ = run_campaign(
                config=config,
                store=store,
                llm=llm,
                parent_slug=args.slug,
                leads_csv_path=args.leads,
                out_dir=args.out_dir,
                sheet_id=args.sheet_id,
                sheet_title=args.sheet_title,
                sheet_share_with=args.sheet_share_with,
                drive_folder_id=args.drive_folder_id,
                gsheets_auth=args.gsheets_auth,
                stages=args.stages,
                headless=not args.headful,
                recipient_mode=args.recipient_mode,
                variant_mode=args.variant_mode,
                output_schema=args.output_schema,
                llm_policy=args.llm_policy,
                enrichment_mode=args.enrichment_mode,
                max_concurrency=args.max_concurrency,
                max_retries=args.max_retries,
                backoff_base_seconds=args.backoff_base_seconds,
                cost_cap_eur=args.cost_cap_eur,
                force_cost_override=args.force_cost_override,
                io_mode=args.io_mode,
                workspace_folder_id=args.workspace_folder_id or config.workspace_folder_id,
            )
            print(f"Campaign completed: {summary.campaign_id}")
            print(f"Companies: {summary.companies_total} | generated: {summary.generated_total} | warnings: {summary.warnings_total}")
            print(f"Local export: {export_path}")
            print(
                "Rows: "
                f"total={summary.rows_total} valid={summary.rows_valid} skipped={summary.rows_skipped} "
                f"ok={summary.rows_generated_ok} failed={summary.rows_failed}"
            )
            print(
                "Costs: "
                f"estimated={summary.estimated_cost_eur:.2f} EUR actual={summary.actual_cost_eur:.2f} EUR"
            )
            return 0

        if args.campaign_command == "status":
            try:
                store = _store(config)
            except RuntimeError as exc:
                print(str(exc))
                return 1
            status = campaign_status(store, args.campaign_id)
            if status is None:
                print("Campaign not found")
                return 1
            print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
            return 0

        if args.campaign_command == "export":
            try:
                store = _store(config)
            except RuntimeError as exc:
                print(str(exc))
                return 1
            output_path = export_campaign(store, args.campaign_id, args.out, output_schema=args.output_schema)
            print(f"Campaign exported: {output_path}")
            return 0

        if args.campaign_command == "publish-sheet":
            csv_path = Path(args.csv)
            if not csv_path.exists():
                print(f"CSV not found: {csv_path}")
                return 1

            rows: list[dict[str, object]] = []
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                sendready_columns = [column for column in (reader.fieldnames or []) if column]
                for row in reader:
                    rows.append(dict(row))
            parent_slug_hint = ""
            campaign_id_hint = ""
            if rows:
                first = rows[0]
                parent_slug_hint = str(first.get("parent_slug") or "").strip()
                campaign_id_hint = str(first.get("campaign_id") or "").strip()

            auth_mode = (args.gsheets_auth or "auto").lower()
            if auth_mode not in {"auto", "service_account", "oauth"}:
                print("gsheets_auth must be one of: auto, service_account, oauth")
                return 1

            service_account_json = config.google_service_account_json
            auth_interactive = False
            if auth_mode == "oauth":
                service_account_json = None
                auth_interactive = True
            elif auth_mode == "service_account":
                if not service_account_json:
                    print("GOOGLE_SERVICE_ACCOUNT_JSON is required for gsheets_auth=service_account")
                    return 1
            else:
                if not service_account_json:
                    print(
                        "Google Sheets auth not configured. "
                        "Set GOOGLE_SERVICE_ACCOUNT_JSON or re-run with --gsheets-auth oauth."
                    )
                    return 1

            result = publish_campaign_to_sheets(
                sheet_id=args.sheet_id,
                sheet_title=args.sheet_title,
                parent_slug=parent_slug_hint or None,
                campaign_id=campaign_id_hint or None,
                sheet_share_with=args.sheet_share_with,
                drive_folder_id=args.drive_folder_id,
                rows=rows,
                sendready_columns=sendready_columns,
                service_account_json=service_account_json,
                output_schema=args.output_schema,
                auth_interactive=auth_interactive,
            )
            print(f"[gsheets] published: {result.spreadsheet_url}")
            print(
                f"[gsheets] rows drafts={result.drafts_rows_written} sendready={result.sendready_rows_written}"
            )
            return 0

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
