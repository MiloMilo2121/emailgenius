from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BrowserSnapshot(BaseModel):
    url: str
    title: str
    text_excerpt: str
    full_text: str
    links: list[str]


class CompanySignals(BaseModel):
    facility_reduction_pct: float | None = None
    process_reduction_pct: float | None = None
    has_esg_report: bool = False
    has_industry40_signals: bool = False
    sector_tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class EligibilityResult(BaseModel):
    eligible: bool
    estimated_credit_rate: int | None
    trigger: str | None
    confidence: float
    rationale: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str = ""


ALLOWED_RESEARCH_SOURCES = ("web", "instagram", "linkedin")
DEFAULT_RESEARCH_SOURCES = ("web",)


class ResearchSource(BaseModel):
    title: str
    url: str
    snippet: str = ""
    source_type: str = ""
    published_at: str | None = None


class DiscoveryContext(BaseModel):
    site_query: str
    site_candidates: list[SearchHit] = Field(default_factory=list)
    selected_site: SearchHit | None = None
    news_query: str = ""
    news_results: list[SearchHit] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    created_at_utc: str
    input_url: str
    company_name: str
    browser_snapshot: BrowserSnapshot
    signals: CompanySignals
    eligibility: EligibilityResult
    outreach_email: str
    discovery: DiscoveryContext | None = None


class ParentProfile(BaseModel):
    slug: str
    company_name: str
    tone: str
    offer_catalog: list[str] = Field(default_factory=list)
    icp: list[str] = Field(default_factory=list)
    proof_points: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    cta_policy: str = "call conoscitiva 20-30 min"
    no_go_claims: list[str] = Field(default_factory=list)
    compliance_notes: list[str] = Field(default_factory=list)
    sender_name: str = ""
    sender_company: str | None = None
    sender_phone: str | None = None
    sender_booking_url: str | None = None
    outreach_seed_template: str = ""
    instantly_intro_template: str = ""
    instantly_cta_template: str = ""


class LeadCompany(BaseModel):
    company_key: str
    company_name: str
    website: str | None
    linkedin_company: str | None
    industry: str | None
    employee_count: int | None
    location: str | None
    keywords: str | None
    tech: str | None
    founded_year: int | None
    evidence: list[str] = Field(default_factory=list)


class LeadContact(BaseModel):
    full_name: str
    title: str | None
    seniority: str | None
    email: str | None
    linkedin_person: str | None
    quality_flag: str | None
    score: float
    is_primary_contact: bool = False
    raw: dict[str, str] = Field(default_factory=dict)


class EnrichmentDossier(BaseModel):
    site_summary: str
    news_items: list[SearchHit] = Field(default_factory=list)
    linkedin_public_summary: str = ""
    pain_hypotheses: list[str] = Field(default_factory=list)
    opportunity_hypotheses: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class DraftEmailVariant(BaseModel):
    variant: str
    subject: str
    body: str
    cta: str
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class ResearchDossier(BaseModel):
    company_name: str
    domain: str | None = None
    company_summary: str = ""
    trigger_event: str = ""
    pain_hypothesis: str = ""
    personalization_angle: str = ""
    key_facts: list[str] = Field(default_factory=list)
    recent_news: list[ResearchSource] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    research_sources: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class InstantlyDraft(BaseModel):
    subject_line: str
    personalization: str
    intro_line: str
    cta_line: str
    body_template: str
    confidence: float = 0.0
    risk_flags: list[str] = Field(default_factory=list)


class ApprovalRecord(BaseModel):
    status: str
    reviewer: str | None = None
    notes: str | None = None
    approved_variant: str | None = None
    updated_at: str | None = None


class SequenceStep(BaseModel):
    step_id: str
    subject: str
    body: str
    goal: str
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class SequenceResult(BaseModel):
    attack_angle: str
    trigger_facts: list[str] = Field(default_factory=list)
    steps: list[SequenceStep] = Field(default_factory=list)
    recommended_next_step: str | None = None
    global_risk_flags: list[str] = Field(default_factory=list)


class CampaignCompanyResult(BaseModel):
    campaign_id: str
    parent_slug: str
    company: LeadCompany
    contact: LeadContact | None
    dossier: EnrichmentDossier
    variants: list[DraftEmailVariant]
    recommended_variant: str
    approval: ApprovalRecord
    sequence_result: SequenceResult | None = None
    research_dossier: ResearchDossier | None = None
    instantly_draft: InstantlyDraft | None = None
    risk_flags: list[str] = Field(default_factory=list)


class CampaignSummary(BaseModel):
    campaign_id: str
    parent_slug: str
    leads_file: str
    sheet_id: str | None
    status: str
    companies_total: int
    generated_total: int
    warnings_total: int
    recipient_mode: str = "company"
    variant_mode: str = "ab"
    output_schema: str = "ab"
    llm_policy: str = "strict"
    io_mode: str = "local"
    workspace_folder_id: str | None = None
    rows_total: int = 0
    rows_valid: int = 0
    rows_skipped: int = 0
    rows_generated_ok: int = 0
    rows_failed: int = 0
    estimated_cost_eur: float = 0.0
    actual_cost_eur: float = 0.0
    research_sources: list[str] = Field(default_factory=list)


JsonDict = dict[str, Any]
