from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..types import (
    CampaignCompanyResult,
    DraftEmailVariant,
    EnrichmentDossier,
    LeadCompany,
    LeadContact,
)


class CampaignState(TypedDict):
    campaign_id: str
    parent_slug: str
    companies: list[LeadCompany]
    current_index: int
    dossiers: dict[str, EnrichmentDossier]
    results: dict[str, CampaignCompanyResult]
    errors: Annotated[list[str], operator.add]


class CompanyState(TypedDict):
    company: LeadCompany
    contact: LeadContact | None
    dossier: EnrichmentDossier | None
    variants: list[DraftEmailVariant]
