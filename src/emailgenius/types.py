from __future__ import annotations

from dataclasses import dataclass, field


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
class AnalysisResult:
    created_at_utc: str
    input_url: str
    company_name: str
    browser_snapshot: BrowserSnapshot
    signals: CompanySignals
    eligibility: EligibilityResult
    outreach_email: str
