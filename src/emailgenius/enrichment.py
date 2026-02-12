from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from .browser import fetch_website_snapshot
from .search import discover_company_and_news, search_news_web
from .types import EnrichmentDossier, LeadCompany, LeadContact, SearchHit
from .utils import compact_lines


async def build_enrichment_dossier(
    *,
    company: LeadCompany,
    contact: LeadContact | None,
    headless: bool = True,
    max_extra_pages: int = 2,
    snapshot_timeout_ms: int = 45000,
) -> tuple[EnrichmentDossier, str | None]:
    website = company.website
    city = _guess_city(company.location)
    news_items: list[SearchHit] = []
    sources: list[str] = []

    if not website:
        _, _, discovered_news, _, selected_site = discover_company_and_news(
            company_name=company.company_name,
            city=city,
            site_max_results=8,
            news_max_results=6,
        )
        website = selected_site.url if selected_site else None
        news_items.extend(discovered_news)
        if selected_site:
            sources.append(selected_site.url)

    site_summary = ""
    evidence: list[str] = []
    pain_hypotheses = _infer_pains(company)
    opportunity_hypotheses = _infer_opportunities(company)

    if website:
        try:
            snapshot = await fetch_website_snapshot(
                url=website,
                headless=headless,
                timeout_ms=snapshot_timeout_ms,
            )
            site_summary = snapshot.text_excerpt[:1200]
            sources.append(snapshot.url)
            evidence.append(f"Homepage title: {snapshot.title}")

            extra_urls = _pick_informative_links(snapshot.links, base_url=website, limit=max_extra_pages)
            for extra_url in extra_urls:
                try:
                    extra_snapshot = await fetch_website_snapshot(
                        url=extra_url,
                        headless=headless,
                        timeout_ms=snapshot_timeout_ms,
                    )
                    sources.append(extra_snapshot.url)
                    evidence.append(f"Pagina rilevata: {extra_snapshot.title}")
                    if len(site_summary) < 2200:
                        site_summary += "\n" + extra_snapshot.text_excerpt[:500]
                except Exception:
                    continue
        except Exception:
            evidence.append("Sito non analizzabile in modo completo")

    if not news_items:
        news_query = f"{company.company_name} {city} news".strip()
        news_items = search_news_web(news_query, max_results=6)

    sources.extend(hit.url for hit in news_items)

    linkedin_summary = _linkedin_summary(company, contact)
    evidence.extend(_company_evidence(company))

    dossier = EnrichmentDossier(
        site_summary=" ".join(site_summary.split())[:2500],
        news_items=news_items,
        linkedin_public_summary=linkedin_summary,
        pain_hypotheses=compact_lines(pain_hypotheses, limit=5),
        opportunity_hypotheses=compact_lines(opportunity_hypotheses, limit=5),
        evidence=compact_lines(evidence, limit=12),
        sources=compact_lines(sources, limit=15),
    )
    return dossier, website


def build_enrichment_dossier_sync(
    *,
    company: LeadCompany,
    contact: LeadContact | None,
    headless: bool = True,
    max_extra_pages: int = 2,
    snapshot_timeout_ms: int = 45000,
) -> tuple[EnrichmentDossier, str | None]:
    return asyncio.run(
        build_enrichment_dossier(
            company=company,
            contact=contact,
            headless=headless,
            max_extra_pages=max_extra_pages,
            snapshot_timeout_ms=snapshot_timeout_ms,
        )
    )


def _guess_city(location: str | None) -> str:
    if not location:
        return ""
    return location.split(",")[0].strip()


def _pick_informative_links(links: list[str], *, base_url: str, limit: int) -> list[str]:
    if not links:
        return []

    host = urlparse(base_url).netloc.lower().replace("www.", "")
    keywords = (
        "about",
        "chi-siamo",
        "azienda",
        "sostenibilita",
        "sustainability",
        "servizi",
        "solutions",
        "news",
    )

    picked: list[str] = []
    for link in links:
        parsed = urlparse(link)
        link_host = parsed.netloc.lower().replace("www.", "")
        if host and link_host and host != link_host:
            continue
        lower_link = link.lower()
        if not any(token in lower_link for token in keywords):
            continue
        if link not in picked:
            picked.append(link)
        if len(picked) >= limit:
            break

    return picked


def _linkedin_summary(company: LeadCompany, contact: LeadContact | None) -> str:
    items: list[str] = []
    if company.linkedin_company:
        items.append(f"LinkedIn aziendale disponibile: {company.linkedin_company}")
    if contact and contact.linkedin_person:
        items.append(f"LinkedIn referente disponibile: {contact.linkedin_person}")
    if not items:
        return "Nessun profilo LinkedIn pubblico disponibile nel dataset."
    return " ".join(items)


def _infer_pains(company: LeadCompany) -> list[str]:
    out: list[str] = []
    keywords = (company.keywords or "").lower()
    industry = (company.industry or "").lower()

    if "manufacturing" in keywords or "machinery" in industry:
        out.append("possibile pressione su efficienza operativa e continuita' produttiva")
    if "quality" in keywords or "iso" in keywords:
        out.append("necessita' di presidiare standard qualita' e compliance")
    if "automation" in keywords or "iot" in keywords:
        out.append("integrazione tra sistemi digitali e processi legacy")
    if "food" in keywords or "pharma" in keywords:
        out.append("tracciabilita' e requisiti normativi stringenti")

    if not out:
        out.append("allineamento tra priorita' commerciali e execution operativa")
    return out


def _infer_opportunities(company: LeadCompany) -> list[str]:
    out: list[str] = []
    keywords = (company.keywords or "").lower()

    if "sustainability" in keywords or "esg" in keywords:
        out.append("valorizzare iniziative ESG con messaggi commerciali misurabili")
    if "innovation" in keywords or "high-tech" in keywords:
        out.append("accelerare time-to-market su offerte ad alto valore")
    if "b2b" in keywords:
        out.append("migliorare posizionamento e conversione su pipeline enterprise")

    out.append("definire quick win con impatto commerciale tracciabile")
    return out


def _company_evidence(company: LeadCompany) -> list[str]:
    items: list[str] = []
    if company.industry:
        items.append(f"Industry: {company.industry}")
    if company.employee_count:
        items.append(f"Employee count stimato: {company.employee_count}")
    if company.location:
        items.append(f"Location: {company.location}")
    if company.founded_year:
        items.append(f"Founded year: {company.founded_year}")
    items.extend(company.evidence)
    return items
