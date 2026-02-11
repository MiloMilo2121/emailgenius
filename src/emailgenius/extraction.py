from __future__ import annotations

import re

from .types import CompanySignals


SECTOR_KEYWORDS = {
    "ceramica": ["ceramica", "piastrelle", "fornace"],
    "carta": ["cartiera", "carta", "cellulosa"],
    "metallurgia": ["acciaio", "fonderia", "metallurgia", "laminazione"],
    "alimentare": ["alimentare", "food", "bevande", "agroalimentare"],
}

ESG_KEYWORDS = [
    "report di sostenibilita",
    "bilancio di sostenibilita",
    "esg",
    "scope 1",
    "scope 2",
    "decarbonizzazione",
]

INDUSTRY40_KEYWORDS = [
    "industria 4.0",
    "interconnessione",
    "mes",
    "scada",
    "iot",
    "digital twin",
]

FACILITY_HINTS = [
    "stabilimento",
    "sito produttivo",
    "impianto",
    "fabbrica",
    "struttura produttiva",
]

PROCESS_HINTS = [
    "processo",
    "linea",
    "reparto",
    "ciclo produttivo",
]


def _normalize_text(text: str) -> str:
    lowered = text.lower()
    lowered = lowered.replace("\u00a0", " ")
    return lowered


def _parse_pct(value: str) -> float:
    return float(value.replace(",", "."))


def _extract_reduction_candidates(text: str) -> list[tuple[float, str]]:
    pattern = re.compile(
        r"(?P<context>.{0,80}?)(?P<pct>\d{1,2}(?:[\.,]\d+)?)\s*%(.{0,80}?)",
        flags=re.IGNORECASE,
    )
    candidates: list[tuple[float, str]] = []
    for match in pattern.finditer(text):
        pct = _parse_pct(match.group("pct"))
        if pct <= 0 or pct > 60:
            continue
        window = match.group(0)
        if any(token in window for token in ("consum", "energet", "efficien", "riduz", "saving")):
            candidates.append((pct, window.strip()))
    return candidates


def infer_company_signals(text: str) -> CompanySignals:
    normalized = _normalize_text(text)

    sector_tags = [
        sector
        for sector, words in SECTOR_KEYWORDS.items()
        if any(word in normalized for word in words)
    ]

    has_esg_report = any(keyword in normalized for keyword in ESG_KEYWORDS)
    has_industry40_signals = any(keyword in normalized for keyword in INDUSTRY40_KEYWORDS)

    facility_values: list[float] = []
    process_values: list[float] = []
    evidence: list[str] = []

    for pct, context in _extract_reduction_candidates(normalized):
        context_compact = " ".join(context.split())
        if any(hint in context for hint in FACILITY_HINTS):
            facility_values.append(pct)
            evidence.append(f"facility_signal={pct}% | {context_compact[:140]}")
            continue
        if any(hint in context for hint in PROCESS_HINTS):
            process_values.append(pct)
            evidence.append(f"process_signal={pct}% | {context_compact[:140]}")
            continue

        # Ambiguous match: assign to the first missing bucket to keep behavior deterministic.
        if not facility_values:
            facility_values.append(pct)
            evidence.append(f"facility_guess={pct}% | {context_compact[:140]}")
        else:
            process_values.append(pct)
            evidence.append(f"process_guess={pct}% | {context_compact[:140]}")

    if has_esg_report:
        evidence.append("Detected ESG/sustainability reporting keywords")
    if has_industry40_signals:
        evidence.append("Detected Industry 4.0 / digitalization keywords")
    if sector_tags:
        evidence.append(f"Detected sector tags: {', '.join(sector_tags)}")

    facility_reduction_pct = max(facility_values) if facility_values else None
    process_reduction_pct = max(process_values) if process_values else None

    return CompanySignals(
        facility_reduction_pct=facility_reduction_pct,
        process_reduction_pct=process_reduction_pct,
        has_esg_report=has_esg_report,
        has_industry40_signals=has_industry40_signals,
        sector_tags=sector_tags,
        evidence=evidence[:15],
    )
