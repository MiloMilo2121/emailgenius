from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BrowserSnapshot:
    url: str
    title: str
    text_excerpt: str
    full_text: str
    links: list[str]


@dataclass(slots=True)
class CompanySignals:
    facility_reduction_pct: float | None = None
    process_reduction_pct: float | None = None
    has_esg_report: bool = False
    has_industry40_signals: bool = False
    sector_tags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EligibilityResult:
    eligible: bool
    estimated_credit_rate: int | None
    trigger: str | None
    confidence: float
    rationale: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass(slots=True)
class DiscoveryContext:
    site_query: str
    site_candidates: list[SearchHit] = field(default_factory=list)
    selected_site: SearchHit | None = None
    news_query: str = ""
    news_results: list[SearchHit] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisResult:
    created_at_utc: str
    input_url: str
    company_name: str
    browser_snapshot: BrowserSnapshot
    signals: CompanySignals
    eligibility: EligibilityResult
    outreach_email: str
    discovery: DiscoveryContext | None = None


@dataclass(slots=True)
class ParentProfile:
    slug: str
    company_name: str
    tone: str
    offer_catalog: list[str] = field(default_factory=list)
    icp: list[str] = field(default_factory=list)
    proof_points: list[str] = field(default_factory=list)
    objections: list[str] = field(default_factory=list)
    cta_policy: str = "call conoscitiva 20-30 min"
    no_go_claims: list[str] = field(default_factory=list)
    compliance_notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LeadCompany:
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
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LeadContact:
    full_name: str
    title: str | None
    seniority: str | None
    email: str | None
    linkedin_person: str | None
    quality_flag: str | None
    score: float
    is_primary_contact: bool = False
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class EnrichmentDossier:
    site_summary: str
    news_items: list[SearchHit] = field(default_factory=list)
    linkedin_public_summary: str = ""
    pain_hypotheses: list[str] = field(default_factory=list)
    opportunity_hypotheses: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DraftEmailVariant:
    variant: str
    subject: str
    body: str
    cta: str
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class ApprovalRecord:
    status: str
    reviewer: str | None = None
    notes: str | None = None
    approved_variant: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class CampaignCompanyResult:
    campaign_id: str
    parent_slug: str
    company: LeadCompany
    contact: LeadContact | None
    dossier: EnrichmentDossier
    variants: list[DraftEmailVariant]
    recommended_variant: str
    approval: ApprovalRecord
    risk_flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CampaignSummary:
    campaign_id: str
    parent_slug: str
    leads_file: str
    sheet_id: str | None
    status: str
    companies_total: int
    generated_total: int
    warnings_total: int


JsonDict = dict[str, Any]
