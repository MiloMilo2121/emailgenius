from __future__ import annotations

import csv
import io
import shutil
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .campaign import run_campaign_legacy
from .config import AppConfig, app_home
from .knowledge import ingest_knowledge_file
from .llm import LLMGateway, render_instantly_body_template
from .storage import PostgresStore
from .types import ParentProfile
from .utils import slugify

INSTANTLY_EXPORT_COLUMNS = [
    "Email",
    "FirstName",
    "LastName",
    "CompanyName",
    "SubjectLine",
    "Personalization",
    "CampaignId",
    "ParentSlug",
]

INSTANTLY_TEMPLATE_BODY = (
    "Ciao {{FirstName}},\n\n"
    "ho guardato il contesto di {{CompanyName}} e mi sembra ci sia un angolo interessante.\n\n"
    "{{Personalization}}\n\n"
    "Se ha senso, ti mando due dettagli operativi in piu.\n\n"
    "{{SenderName}}\n"
    "{{SenderCompany}}"
)


def create_app(
    config: AppConfig | None = None,
    *,
    store: PostgresStore | None = None,
    llm: LLMGateway | None = None,
    store_factory: Callable[[], PostgresStore] | None = None,
    llm_factory: Callable[[], LLMGateway] | None = None,
) -> FastAPI:
    resolved_config = config or AppConfig.from_env()
    resolved_store_factory = store_factory or (lambda: store or _store_from_config(resolved_config))
    resolved_llm_factory = llm_factory or (lambda: llm or _llm_from_config(resolved_config))

    templates_dir = Path(__file__).with_name("templates")
    static_dir = Path(__file__).with_name("static")
    reports_dir = app_home() / "web-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="EmailGenius App")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/reports", StaticFiles(directory=reports_dir), name="reports")
    app.state.config = resolved_config
    app.state.store_factory = resolved_store_factory
    app.state.llm_factory = resolved_llm_factory
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.jobs = _JobRegistry()

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def dashboard(request: Request) -> HTMLResponse:
        store = _get_store(request)
        return _templates(request).TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "page_title": "Dashboard",
                "ui": _ui_state(request),
                "config_summary": _config_summary(request.app.state.config),
                "profile_defaults": _profile_defaults(),
                "parents": store.list_parent_profiles(),
                "active_parent_slug": store.get_active_parent_slug(),
                "campaigns": store.list_campaign_summaries(limit=12),
                "jobs": request.app.state.jobs.list_recent(limit=8),
            },
        )

    @app.get("/parents", response_class=HTMLResponse, name="parents", response_model=None)
    def parents(request: Request) -> HTMLResponse:
        return _templates(request).TemplateResponse(
            request,
            "parent_form.html",
            {
                "request": request,
                "page_title": "Nuovo parent",
                "ui": _ui_state(request),
                "parent": None,
            },
        )

    @app.get("/parents/new", response_class=HTMLResponse, response_model=None)
    def parent_new(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/parents", status_code=303)

    @app.post("/parents", response_model=None)
    async def parent_save(
        request: Request,
        slug: str = Form(""),
        company_name: str = Form(...),
        tone: str = Form(...),
        offer_catalog: str = Form(""),
        icp: str = Form(""),
        proof_points: str = Form(""),
        objections: str = Form(""),
        cta_policy: str = Form("call conoscitiva 20-30 min"),
        no_go_claims: str = Form(""),
        compliance_notes: str = Form(""),
        sender_name: str = Form(""),
        sender_company: str = Form(""),
        sender_phone: str = Form(""),
        sender_booking_url: str = Form(""),
        outreach_seed_template: str = Form(""),
        instantly_intro_template: str = Form(""),
        instantly_cta_template: str = Form(""),
        set_active: str = Form(""),
    ) -> Response:
        try:
            profile = _parent_profile_from_form(
                slug=slug,
                company_name=company_name,
                tone=tone,
                offer_catalog=offer_catalog,
                icp=icp,
                proof_points=proof_points,
                objections=objections,
                cta_policy=cta_policy,
                no_go_claims=no_go_claims,
                compliance_notes=compliance_notes,
                sender_name=sender_name,
                sender_company=sender_company,
                sender_phone=sender_phone,
                sender_booking_url=sender_booking_url,
                outreach_seed_template=outreach_seed_template,
                instantly_intro_template=instantly_intro_template,
                instantly_cta_template=instantly_cta_template,
            )
            _get_store(request).upsert_parent_profile(profile, set_active=bool(set_active))
        except Exception as exc:
            parent = _safe_parent_from_partial_form(
                slug=slug,
                company_name=company_name,
                tone=tone,
                offer_catalog=offer_catalog,
                icp=icp,
                proof_points=proof_points,
                objections=objections,
                cta_policy=cta_policy,
                no_go_claims=no_go_claims,
                compliance_notes=compliance_notes,
                sender_name=sender_name,
                sender_company=sender_company,
                sender_phone=sender_phone,
                sender_booking_url=sender_booking_url,
                outreach_seed_template=outreach_seed_template,
                instantly_intro_template=instantly_intro_template,
                instantly_cta_template=instantly_cta_template,
            )
            return _templates(request).TemplateResponse(
                request,
                "parent_form.html",
                {
                    "request": request,
                    "page_title": "Nuovo parent",
                    "ui": {"message": "", "error": str(exc)},
                    "parent": parent,
                },
                status_code=400,
            )
        return RedirectResponse(url=f"/parents/{profile.slug}?message=Parent+salvato", status_code=303)

    @app.get("/parents/{slug}", response_class=HTMLResponse, response_model=None)
    def parent_detail(request: Request, slug: str) -> HTMLResponse:
        store = _get_store(request)
        parent = store.get_parent_profile(slug)
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent not found")
        return _templates(request).TemplateResponse(
            request,
            "parent_detail.html",
            {
                "request": request,
                "page_title": parent.company_name,
                "ui": _ui_state(request),
                "parent": parent,
                "knowledge_docs": store.list_knowledge_documents(slug),
                "campaigns": store.list_campaign_summaries(limit=8, parent_slug=slug),
            },
        )

    @app.post("/parents/{slug}/activate", response_model=None)
    def parent_activate(request: Request, slug: str) -> RedirectResponse:
        _get_store(request).set_active_parent(slug)
        return RedirectResponse(url=f"/parents/{slug}?message=Parent+attivato", status_code=303)

    @app.post("/knowledge/upload", response_model=None)
    async def knowledge_upload(
        request: Request,
        slug: str = Form(...),
        kind: str = Form("marketing"),
        files: list[UploadFile] = File(...),
    ) -> RedirectResponse:
        store = _get_store(request)
        if store.get_parent_profile(slug) is None:
            raise HTTPException(status_code=404, detail="Parent not found")
        llm = _get_llm(request)
        uploads_dir = app_home() / "web-uploads" / slug / "knowledge"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        for upload in files:
            suffix = Path(upload.filename or "knowledge.txt").suffix or ".txt"
            local_path = uploads_dir / f"{uuid.uuid4().hex}{suffix}"
            with local_path.open("wb") as handle:
                shutil.copyfileobj(upload.file, handle)
            ingest_knowledge_file(
                store=store,
                llm=llm,
                parent_slug=slug,
                file_path=str(local_path),
                kind=kind,
            )
        return RedirectResponse(url=f"/parents/{slug}?message=Knowledge+indicizzata", status_code=303)

    @app.post("/campaigns/local", response_model=None)
    async def launch_local_campaign(
        request: Request,
        slug: str = Form(...),
        llm_policy: str = Form("strict"),
        cost_cap_eur: float = Form(50.0),
        force_cost_override: str = Form(""),
        research_sources: list[str] | None = Form(None),
        leads_file: UploadFile = File(...),
    ) -> RedirectResponse:
        resolved_research_sources = list(research_sources or ["web"])
        uploads_dir = app_home() / "web-uploads" / "csv"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(leads_file.filename or "leads.csv").suffix or ".csv"
        local_path = uploads_dir / f"{uuid.uuid4().hex}{suffix}"
        with local_path.open("wb") as handle:
            shutil.copyfileobj(leads_file.file, handle)

        job = request.app.state.jobs.create(
            label=f"Campagna locale {slug}",
            kind="local_campaign",
            parent_slug=slug,
            payload_json={
                "leads_csv_path": str(local_path),
                "llm_policy": llm_policy,
                "research_sources": resolved_research_sources,
            },
        )
        _launch_background_job(
            request.app,
            job.job_id,
            _run_local_campaign_job,
            config=request.app.state.config,
            parent_slug=slug,
            leads_csv_path=str(local_path),
            llm_policy=llm_policy,
            cost_cap_eur=float(cost_cap_eur),
            force_cost_override=bool(force_cost_override),
            research_sources=resolved_research_sources,
        )
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @app.post("/campaigns/drive", response_model=None)
    def launch_drive_campaign(
        request: Request,
        slug: str = Form(""),
        workspace_folder_id: str = Form(""),
        gsheets_auth: str = Form("auto"),
        llm_policy: str = Form("strict"),
        cost_cap_eur: float = Form(50.0),
        force_cost_override: str = Form(""),
    ) -> RedirectResponse:
        store = _get_store(request)
        resolved_slug = slug.strip() or _resolve_ui_parent_slug(store)
        resolved_workspace_folder_id = workspace_folder_id.strip() or request.app.state.config.workspace_folder_id or ""
        job = request.app.state.jobs.create(
            label=f"Sync Drive {resolved_slug}",
            kind="workspace_sync",
            parent_slug=resolved_slug,
            payload_json={
                "workspace_folder_id": resolved_workspace_folder_id,
                "gsheets_auth": gsheets_auth,
                "llm_policy": llm_policy,
            },
        )
        _launch_background_job(
            request.app,
            job.job_id,
            _run_workspace_sync_job,
            config=request.app.state.config,
            parent_slug=resolved_slug,
            workspace_folder_id=resolved_workspace_folder_id,
            llm_policy=llm_policy,
            gsheets_auth=gsheets_auth,
            cost_cap_eur=float(cost_cap_eur),
            force_cost_override=bool(force_cost_override),
        )
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_model=None)
    def job_detail(request: Request, job_id: str) -> Response:
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if "application/json" in (request.headers.get("accept") or ""):
            return JSONResponse(job.to_dict())
        return _templates(request).TemplateResponse(
            request,
            "job_detail.html",
            {
                "request": request,
                "page_title": f"Job {job.job_id[:8]}",
                "ui": _ui_state(request),
                "job": job,
            },
        )

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse, response_model=None)
    def campaign_detail(request: Request, campaign_id: str) -> HTMLResponse:
        store = _get_store(request)
        summary = store.get_campaign_summary(campaign_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        parent = store.get_parent_profile(str(summary.get("parent_slug") or ""))
        raw_records = store.list_campaign_records(campaign_id)
        records = [_campaign_record_view(row) for row in raw_records]
        instantly_preview = _build_instantly_preview(
            campaign_id=campaign_id,
            parent=parent,
            records=raw_records,
        )
        return _templates(request).TemplateResponse(
            request,
            "campaign_detail.html",
            {
                "request": request,
                "page_title": f"Campaign {campaign_id[:8]}",
                "ui": _ui_state(request),
                "summary": summary,
                "records": records,
                "parent": parent,
                "instantly_preview": instantly_preview,
                "instantly_download_url": str(
                    request.url_for("campaign_instantly_export", campaign_id=campaign_id)
                ),
            },
        )

    @app.get("/campaigns/{campaign_id}/export/instantly.csv", name="campaign_instantly_export", response_model=None)
    def campaign_instantly_export(request: Request, campaign_id: str) -> Response:
        store = _get_store(request)
        summary = store.get_campaign_summary(campaign_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        parent = store.get_parent_profile(str(summary.get("parent_slug") or ""))
        raw_records = store.list_campaign_records(campaign_id)
        rows = _build_instantly_export_rows(
            campaign_id=campaign_id,
            parent=parent,
            records=raw_records,
        )
        csv_text = _render_csv(rows, fieldnames=INSTANTLY_EXPORT_COLUMNS)
        filename = f"campaign-{campaign_id}-instantly.csv"
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    return app


@dataclass(slots=True)
class AppJob:
    job_id: str
    label: str
    kind: str
    parent_slug: str
    status: str
    started_at: str
    updated_at: str
    payload_json: dict[str, object]
    message: str = ""
    error: str = ""
    error_message: str = ""
    campaign_id: str | None = None
    export_path: str | None = None
    retry_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, AppJob] = {}
        self._lock = threading.Lock()

    def create(self, *, label: str, kind: str, parent_slug: str, payload_json: dict[str, object]) -> AppJob:
        now = _utc_now_iso()
        job = AppJob(
            job_id=uuid.uuid4().hex,
            label=label,
            kind=kind,
            parent_slug=parent_slug,
            status="QUEUED",
            started_at=now,
            updated_at=now,
            payload_json=payload_json,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def mark_running(self, job_id: str) -> None:
        self._patch(job_id, status="RUNNING")

    def mark_completed(
        self,
        job_id: str,
        *,
        message: str,
        campaign_id: str | None,
        export_path: str | None,
    ) -> None:
        self._patch(job_id, status="COMPLETED", message=message, campaign_id=campaign_id, export_path=export_path)

    def mark_failed(self, job_id: str, *, error: str) -> None:
        self._patch(job_id, status="FAILED", error=error, error_message=error)

    def get(self, job_id: str) -> AppJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, *, limit: int = 10) -> list[AppJob]:
        with self._lock:
            items = list(self._jobs.values())
        items.sort(key=lambda item: item.started_at, reverse=True)
        return items[: max(1, limit)]

    def _patch(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = _utc_now_iso()


JobRegistry = _JobRegistry


def _launch_background_job(
    app: FastAPI,
    job_id: str,
    job_fn: Callable[..., tuple[str | None, str | None]],
    **kwargs: object,
) -> None:
    registry: _JobRegistry = app.state.jobs

    def runner() -> None:
        registry.mark_running(job_id)
        try:
            campaign_id, export_path = job_fn(app=app, **kwargs)
        except Exception as exc:
            error_text = f"{exc}\n\n{traceback.format_exc(limit=6)}"
            registry.mark_failed(job_id, error=error_text[:3000])
            return
        registry.mark_completed(
            job_id,
            message="Operazione completata.",
            campaign_id=campaign_id,
            export_path=export_path,
        )

    threading.Thread(target=runner, name=f"emailgenius-job-{job_id}", daemon=True).start()


def _run_local_campaign_job(
    *,
    app: FastAPI,
    config: AppConfig,
    parent_slug: str,
    leads_csv_path: str,
    llm_policy: str,
    cost_cap_eur: float,
    force_cost_override: bool,
    research_sources: list[str],
) -> tuple[str | None, str | None]:
    summary, export_path, _ = run_campaign_legacy(
        config=config,
        store=app.state.store_factory(),
        llm=app.state.llm_factory(),
        parent_slug=parent_slug,
        leads_csv_path=leads_csv_path,
        out_dir=str(app_home() / "web-reports"),
        sheet_id=None,
        gsheets_auth="auto",
        stages="all",
        headless=True,
        recipient_mode="row",
        variant_mode="ab",
        output_schema="ab",
        llm_policy=llm_policy,
        enrichment_mode="auto",
        max_concurrency=2,
        max_retries=3,
        backoff_base_seconds=1.0,
        cost_cap_eur=cost_cap_eur,
        force_cost_override=force_cost_override,
        io_mode="local",
        workspace_folder_id=None,
        research_sources=research_sources,
    )
    return summary.campaign_id, str(export_path)


def _run_workspace_sync_job(
    *,
    app: FastAPI,
    config: AppConfig,
    parent_slug: str,
    workspace_folder_id: str,
    llm_policy: str,
    gsheets_auth: str,
    cost_cap_eur: float,
    force_cost_override: bool,
) -> tuple[str | None, str | None]:
    summary, export_path, _ = run_campaign_legacy(
        config=config,
        store=app.state.store_factory(),
        llm=app.state.llm_factory(),
        parent_slug=parent_slug,
        leads_csv_path=None,
        out_dir=str(app_home() / "web-reports"),
        sheet_id=None,
        gsheets_auth=gsheets_auth,
        stages="all",
        headless=True,
        recipient_mode="row",
        variant_mode="ab",
        output_schema="ab",
        llm_policy=llm_policy,
        enrichment_mode="auto",
        max_concurrency=1,
        max_retries=3,
        backoff_base_seconds=1.0,
        cost_cap_eur=cost_cap_eur,
        force_cost_override=force_cost_override,
        io_mode="drive",
        workspace_folder_id=workspace_folder_id or config.workspace_folder_id,
    )
    return summary.campaign_id, str(export_path)


def _store_from_config(config: AppConfig) -> PostgresStore:
    store = PostgresStore(config.database_url)
    store.migrate()
    return store


def _llm_from_config(config: AppConfig) -> LLMGateway:
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


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _get_store(request: Request) -> PostgresStore:
    return request.app.state.store_factory()


def _get_llm(request: Request) -> LLMGateway:
    return request.app.state.llm_factory()


def _ui_state(request: Request) -> dict[str, str]:
    return {
        "message": request.query_params.get("message") or "",
        "error": request.query_params.get("error") or "",
    }


def _config_summary(config: AppConfig) -> dict[str, object]:
    database_target = "Postgres remoto"
    database_url = (config.database_url or "").lower()
    if "localhost" in database_url or "127.0.0.1" in database_url:
        database_target = "Postgres locale"
    return {
        "data_home": str(app_home()),
        "database_target": database_target,
        "workspace_folder_id": config.workspace_folder_id or "",
        "llm_configured": bool(config.openai_api_key or config.openai_fallback_api_key),
        "research_configured": bool(config.exa_api_key),
        "google_configured": bool(config.google_service_account_json),
    }


def _profile_defaults() -> dict[str, str]:
    return {
        "tone": "formale-consulenziale",
        "cta_policy": "call conoscitiva 20-30 min",
        "sender_name": "Team EmailGenius",
        "sender_company": "",
        "sender_phone": "",
        "sender_booking_url": "",
        "outreach_seed_template": (
            "Ciao {{first_name}},\n\n"
            "scrivo per condividere una valutazione preliminare utile per {{company_name}}.\n\n"
            "Se vuoi, possiamo sentirci 20-30 minuti per capire se ci sono margini reali di miglioramento.\n\n"
            "{{sender_name}}\n"
            "{{sender_company}}"
        ),
        "instantly_intro_template": (
            "Ciao {{first_name}},\n\n"
            "scrivo perche' lavoriamo con aziende come {{company_name}} su outreach B2B molto mirato."
        ),
        "instantly_cta_template": (
            "Se ha senso, posso mandarti 2 spunti concreti su come lo imposteremmo per voi.\n\n"
            "{{sender_name}}\n"
            "{{sender_company}}"
        ),
    }


def _parent_profile_from_form(
    *,
    slug: str,
    company_name: str,
    tone: str,
    offer_catalog: str,
    icp: str,
    proof_points: str,
    objections: str,
    cta_policy: str,
    no_go_claims: str,
    compliance_notes: str,
    sender_name: str,
    sender_company: str,
    sender_phone: str,
    sender_booking_url: str,
    outreach_seed_template: str,
    instantly_intro_template: str,
    instantly_cta_template: str,
) -> ParentProfile:
    resolved_slug = slugify(slug or company_name)
    if not resolved_slug:
        raise ValueError("Slug o company name obbligatori.")
    defaults = _profile_defaults()
    profile = ParentProfile(
        slug=resolved_slug,
        company_name=company_name.strip(),
        tone=tone.strip(),
        offer_catalog=_split_lines(offer_catalog),
        icp=_split_lines(icp),
        proof_points=_split_lines(proof_points),
        objections=_split_lines(objections),
        cta_policy=cta_policy.strip(),
        no_go_claims=_split_lines(no_go_claims),
        compliance_notes=_split_lines(compliance_notes),
        sender_name=sender_name.strip(),
        sender_company=sender_company.strip() or None,
        sender_phone=sender_phone.strip() or None,
        sender_booking_url=sender_booking_url.strip() or None,
        outreach_seed_template=outreach_seed_template.strip() or defaults["outreach_seed_template"],
        instantly_intro_template=instantly_intro_template.strip() or defaults["instantly_intro_template"],
        instantly_cta_template=instantly_cta_template.strip() or defaults["instantly_cta_template"],
    )
    if not profile.company_name:
        raise ValueError("Company name obbligatorio.")
    if not profile.tone:
        raise ValueError("Tone obbligatorio.")
    if not profile.offer_catalog:
        raise ValueError("Offer catalog obbligatorio.")
    if not profile.icp:
        raise ValueError("ICP obbligatorio.")
    if not profile.cta_policy:
        raise ValueError("CTA policy obbligatoria.")
    if not profile.sender_name:
        raise ValueError("Sender name obbligatorio.")
    if not profile.outreach_seed_template:
        raise ValueError("Seed template obbligatorio.")
    if not profile.instantly_intro_template:
        raise ValueError("Instantly intro obbligatoria.")
    if not profile.instantly_cta_template:
        raise ValueError("Instantly CTA obbligatoria.")
    return profile


def _safe_parent_from_partial_form(**kwargs: str) -> ParentProfile:
    try:
        return _parent_profile_from_form(**kwargs)
    except Exception:
        return ParentProfile(
            slug=slugify(kwargs.get("slug") or kwargs.get("company_name") or "draft-parent"),
            company_name=kwargs.get("company_name") or "",
            tone=kwargs.get("tone") or "",
            offer_catalog=_split_lines(kwargs.get("offer_catalog") or ""),
            icp=_split_lines(kwargs.get("icp") or ""),
            proof_points=_split_lines(kwargs.get("proof_points") or ""),
            objections=_split_lines(kwargs.get("objections") or ""),
            cta_policy=kwargs.get("cta_policy") or "",
            no_go_claims=_split_lines(kwargs.get("no_go_claims") or ""),
            compliance_notes=_split_lines(kwargs.get("compliance_notes") or ""),
            sender_name=kwargs.get("sender_name") or "",
            sender_company=kwargs.get("sender_company") or None,
            sender_phone=kwargs.get("sender_phone") or None,
            sender_booking_url=kwargs.get("sender_booking_url") or None,
            outreach_seed_template=kwargs.get("outreach_seed_template") or "",
            instantly_intro_template=kwargs.get("instantly_intro_template") or "",
            instantly_cta_template=kwargs.get("instantly_cta_template") or "",
        )


def _campaign_record_view(record: dict[str, object]) -> dict[str, object]:
    payload = _payload_json(record)
    instantly_draft = _instantly_draft_payload(payload)
    return {
        **record,
        "final_subject": str(payload.get("final_subject") or ""),
        "final_body": str(payload.get("final_body") or ""),
        "instantly_subject": str(instantly_draft.get("subject_line") or payload.get("SubjectLine") or ""),
        "instantly_personalization": str(instantly_draft.get("personalization") or payload.get("Personalization") or ""),
    }


def _build_instantly_preview(
    *,
    campaign_id: str,
    parent: ParentProfile | None,
    records: list[dict[str, object]],
) -> dict[str, object]:
    export_rows = _build_instantly_export_rows(
        campaign_id=campaign_id,
        parent=parent,
        records=records,
    )
    return {
        "columns": INSTANTLY_EXPORT_COLUMNS,
        "template_subject": "{{SubjectLine}}",
        "template_body": _instantly_template_body(parent),
        "signature_name": (parent.sender_name if parent else "") or "",
        "signature_company": (parent.sender_company if parent and parent.sender_company else (parent.company_name if parent else "")),
        "rows": export_rows[:3],
    }


def _build_instantly_export_rows(
    *,
    campaign_id: str,
    parent: ParentProfile | None,
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in records:
        payload = _payload_json(record)
        contact_payload = payload.get("contact")
        company_payload = payload.get("company")
        dossier_payload = payload.get("dossier")
        sequence_payload = payload.get("sequence_result")
        contact = contact_payload if isinstance(contact_payload, dict) else {}
        company = company_payload if isinstance(company_payload, dict) else {}
        dossier = dossier_payload if isinstance(dossier_payload, dict) else {}
        sequence_result = sequence_payload if isinstance(sequence_payload, dict) else {}
        sequence_steps = sequence_result.get("steps") if isinstance(sequence_result.get("steps"), list) else []

        email = _first_non_empty(
            str(record.get("contact_email") or ""),
            str((contact or {}).get("email") or ""),
        )
        full_name = _first_non_empty(
            str(record.get("contact_name") or ""),
            str((contact or {}).get("full_name") or ""),
        )
        first_name, last_name = _split_full_name(full_name)
        company_name = _first_non_empty(
            str(record.get("company_name") or ""),
            str((company or {}).get("company_name") or ""),
        )
        instantly_payload = _instantly_draft_payload(payload)
        first_step_subject = ""
        if sequence_steps:
            first_step = sequence_steps[0]
            if isinstance(first_step, dict):
                first_step_subject = str(first_step.get("subject") or "").strip()
        subject_line = _first_non_empty(
            str(instantly_payload.get("subject_line") or ""),
            str(payload.get("final_subject") or ""),
            str(payload.get("recommended_subject") or ""),
            first_step_subject,
        )
        personalization = _build_personalization_snippet(
            company_name=company_name,
            payload=payload,
        )
        if instantly_payload.get("personalization"):
            personalization = str(instantly_payload.get("personalization") or "").strip()

        rows.append(
            {
                "Email": email,
                "FirstName": first_name,
                "LastName": last_name,
                "CompanyName": company_name,
                "SubjectLine": subject_line,
                "Personalization": personalization,
                "CampaignId": campaign_id,
                "ParentSlug": str(record.get("parent_slug") or (parent.slug if parent else "") or ""),
            }
        )

    return rows


def _build_personalization_snippet(*, company_name: str, payload: dict[str, object]) -> str:
    instantly_payload = _instantly_draft_payload(payload)
    text = str(instantly_payload.get("personalization") or "").strip()
    if text:
        return text
    sequence_result = payload.get("sequence_result")
    if isinstance(sequence_result, dict):
        trigger_facts = sequence_result.get("trigger_facts")
        if isinstance(trigger_facts, list):
            for item in trigger_facts:
                text = str(item or "").strip()
                if text:
                    return f"Ho visto il segnale recente su {text} e mi sembra il momento giusto per parlarne."
        attack_angle = str(sequence_result.get("attack_angle") or "").strip()
        if attack_angle:
            return f"Mi ha colpito l'angolo {attack_angle} su {company_name}."

    dossier = payload.get("dossier")
    if isinstance(dossier, dict):
        news_items = dossier.get("news_items")
        if isinstance(news_items, list):
            for item in news_items:
                title = str(item.get("title") or "").strip() if isinstance(item, dict) else ""
                if title:
                    return f"Ho visto la notizia '{title}' e ho preparato un'idea molto concreta per voi."
        evidence = dossier.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                text = str(item or "").strip()
                if text:
                    return f"Ho preso spunto da questo segnale su {company_name}: {text}."
        site_summary = str(dossier.get("site_summary") or "").strip()
        if site_summary:
            return f"Ho guardato il contesto di {company_name} e ci sono un paio di segnali interessanti."

    company = payload.get("company")
    if isinstance(company, dict):
        industry = str(company.get("industry") or "").strip()
        location = str(company.get("location") or "").strip()
        if industry or location:
            chunk = " / ".join(part for part in [industry, location] if part)
            return f"Ho visto che operate in {chunk} e mi sembra un contesto molto specifico."

    return f"Ho preparato un angolo personalizzato per {company_name}."


def _instantly_template_body(parent: ParentProfile | None) -> str:
    if parent:
        return render_instantly_body_template(parent, _preview_company(parent), None)
    sender_name = (parent.sender_name if parent else "") or "Team EmailGenius"
    sender_company = (parent.sender_company if parent and parent.sender_company else (parent.company_name if parent else ""))
    return INSTANTLY_TEMPLATE_BODY.replace("{{SenderName}}", sender_name).replace("{{SenderCompany}}", sender_company)


def _render_csv(rows: list[dict[str, object]], *, fieldnames: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _split_full_name(value: str) -> tuple[str, str]:
    parts = [part for part in value.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _first_non_empty(*values: str) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _preview_company(parent: ParentProfile):
    from .types import LeadCompany

    return LeadCompany(
        company_key=parent.slug,
        company_name="{{CompanyName}}",
        website=None,
        linkedin_company=None,
        industry=None,
        employee_count=None,
        location=None,
        keywords=None,
        tech=None,
        founded_year=None,
    )


def _payload_json(record: dict[str, object]) -> dict[str, object]:
    payload = record.get("payload_json")
    return payload if isinstance(payload, dict) else {}


def _instantly_draft_payload(payload: dict[str, object]) -> dict[str, object]:
    instantly_payload = payload.get("instantly_draft")
    return instantly_payload if isinstance(instantly_payload, dict) else {}


def _resolve_ui_parent_slug(store: PostgresStore) -> str:
    active = store.get_active_parent_slug()
    if active:
        return active
    parents = store.list_parent_profiles()
    if parents:
        return parents[0].slug
    raise ValueError("Nessun parent registrato.")


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
