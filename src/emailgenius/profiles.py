from __future__ import annotations

from pathlib import Path

import yaml

from .types import ParentProfile
from .utils import ensure_list, slugify


REQUIRED_KEYS = {
    "company_name",
    "tone",
    "offer_catalog",
    "icp",
    "proof_points",
    "objections",
    "cta_policy",
    "no_go_claims",
    "compliance_notes",
}


def load_parent_profile(profile_path: str | Path, *, slug_override: str | None = None) -> ParentProfile:
    path = Path(profile_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Parent profile must be a YAML object.")

    missing = sorted(key for key in REQUIRED_KEYS if key not in payload)
    if missing:
        raise ValueError(f"Missing required profile keys: {', '.join(missing)}")

    raw_slug = slug_override or str(payload.get("slug") or "").strip()
    slug = slugify(raw_slug or str(payload["company_name"]))

    profile = ParentProfile(
        slug=slug,
        company_name=str(payload["company_name"]).strip(),
        tone=str(payload["tone"]).strip(),
        offer_catalog=ensure_list(payload.get("offer_catalog")),
        icp=ensure_list(payload.get("icp")),
        proof_points=ensure_list(payload.get("proof_points")),
        objections=ensure_list(payload.get("objections")),
        cta_policy=str(payload.get("cta_policy") or "call conoscitiva 20-30 min").strip(),
        no_go_claims=ensure_list(payload.get("no_go_claims")),
        compliance_notes=ensure_list(payload.get("compliance_notes")),
    )

    _validate_parent_profile(profile)
    return profile


def _validate_parent_profile(profile: ParentProfile) -> None:
    if not profile.company_name:
        raise ValueError("company_name cannot be empty")
    if not profile.tone:
        raise ValueError("tone cannot be empty")
    if not profile.offer_catalog:
        raise ValueError("offer_catalog cannot be empty")
    if not profile.icp:
        raise ValueError("icp cannot be empty")
    if not profile.cta_policy:
        raise ValueError("cta_policy cannot be empty")


def parent_profile_to_dict(profile: ParentProfile) -> dict[str, object]:
    return {
        "slug": profile.slug,
        "company_name": profile.company_name,
        "tone": profile.tone,
        "offer_catalog": profile.offer_catalog,
        "icp": profile.icp,
        "proof_points": profile.proof_points,
        "objections": profile.objections,
        "cta_policy": profile.cta_policy,
        "no_go_claims": profile.no_go_claims,
        "compliance_notes": profile.compliance_notes,
    }


def parent_profile_from_dict(payload: dict[str, object]) -> ParentProfile:
    return ParentProfile(
        slug=str(payload["slug"]),
        company_name=str(payload["company_name"]),
        tone=str(payload["tone"]),
        offer_catalog=ensure_list(payload.get("offer_catalog")),
        icp=ensure_list(payload.get("icp")),
        proof_points=ensure_list(payload.get("proof_points")),
        objections=ensure_list(payload.get("objections")),
        cta_policy=str(payload.get("cta_policy") or "call conoscitiva 20-30 min"),
        no_go_claims=ensure_list(payload.get("no_go_claims")),
        compliance_notes=ensure_list(payload.get("compliance_notes")),
    )
