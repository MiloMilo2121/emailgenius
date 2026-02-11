from __future__ import annotations

import re


DEFAULT_FORBIDDEN_PATTERNS = {
    "claim_guaranteed": re.compile(r"\b(garantit[oaie]|garanzia totale)\b", re.IGNORECASE),
    "claim_absolute": re.compile(r"\b(100%|sempre|mai)\b", re.IGNORECASE),
    "claim_zero_risk": re.compile(r"\b(senza rischi|rischio zero)\b", re.IGNORECASE),
    "claim_unique": re.compile(r"\b(unic[oaie] sul mercato)\b", re.IGNORECASE),
    "claim_immediate": re.compile(r"\b(risultati immediati|subito)\b", re.IGNORECASE),
}


def apply_claim_guard(text: str, no_go_claims: list[str]) -> tuple[str, list[str]]:
    flagged: list[str] = []
    cleaned = text

    for name, pattern in DEFAULT_FORBIDDEN_PATTERNS.items():
        if pattern.search(cleaned):
            flagged.append(name)

    for item in no_go_claims:
        token = item.strip()
        if not token:
            continue
        if re.search(re.escape(token), cleaned, flags=re.IGNORECASE):
            flagged.append(f"no_go:{token}")
            cleaned = re.sub(re.escape(token), "[claim-rimosso]", cleaned, flags=re.IGNORECASE)

    # Soft normalization for risky absolute phrasing.
    cleaned = re.sub(r"\bgarantiamo\b", "puntiamo a", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bgarantito\b", "stimato", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bsenza rischi\b", "con rischio controllato", cleaned, flags=re.IGNORECASE)

    dedup_flags = sorted(set(flagged))
    return cleaned, dedup_flags
