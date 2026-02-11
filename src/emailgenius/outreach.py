from __future__ import annotations

from .types import CompanySignals, EligibilityResult


def generate_outreach_email(
    *,
    company_name: str,
    website_url: str,
    signals: CompanySignals,
    eligibility: EligibilityResult,
) -> str:
    subject = f"Valutazione preliminare Transizione 5.0 per {company_name}"  # nosec B106

    intro = (
        f"Gentile team {company_name},\n\n"
        f"abbiamo analizzato informazioni pubbliche disponibili sul vostro sito ({website_url}) "
        "per stimare opportunita' di efficientamento energetico legate a Transizione 5.0.\n"
    )

    signal_lines: list[str] = []
    if signals.facility_reduction_pct is not None:
        signal_lines.append(
            f"- Riduzione stimata consumi struttura: {signals.facility_reduction_pct:.1f}%"
        )
    if signals.process_reduction_pct is not None:
        signal_lines.append(
            f"- Riduzione stimata consumi processo: {signals.process_reduction_pct:.1f}%"
        )
    if signals.has_esg_report:
        signal_lines.append("- Presenza segnali ESG/sostenibilita' pubblici")
    if signals.has_industry40_signals:
        signal_lines.append("- Presenza segnali di maturita' Industria 4.0")
    if signals.sector_tags:
        signal_lines.append(f"- Settore rilevato: {', '.join(signals.sector_tags)}")

    if not signal_lines:
        signal_lines.append("- Segnali pubblici limitati: suggerita analisi tecnica dedicata")

    if eligibility.eligible and eligibility.estimated_credit_rate is not None:
        proposal = (
            "\nSulla base dei segnali disponibili, emerge una potenziale eleggibilita' "
            f"alla fascia credito d'imposta stimata del {eligibility.estimated_credit_rate}%.\n"
            "Proponiamo una sessione tecnica di 30 minuti per validare i dati su base impiantistica "
            "e definire un business case con stima ROI e roadmap operativa.\n"
        )
    else:
        proposal = (
            "\nI segnali pubblici non sono ancora sufficienti per stimare una fascia incentivo affidabile.\n"
            "Possiamo eseguire una pre-analisi guidata su dati energetici reali per identificare gap e opportunita'.\n"
        )

    close = (
        "\nResto a disposizione per condividere metodologia, assunzioni e limiti dell'analisi preliminare.\n\n"
        "Cordiali saluti,\n"
        "Consulente AI - EmailGenius"
    )

    body = intro + "\n".join(signal_lines) + proposal + close
    return f"Oggetto: {subject}\n\n{body}"
