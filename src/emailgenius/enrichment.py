from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from .browser import fetch_website_snapshot
from .search import collect_company_news, discover_company_and_news, is_event_news_hit
from .types import EnrichmentDossier, LeadCompany, LeadContact, SearchHit
from .utils import compact_lines


@dataclass(slots=True)
class NebulaEnrichmentMachine:
    codename: str
    score: float
    depth: str
    personalization_hooks: list[str]
    value_angles: list[str]
    evidence_highlights: list[str]
    missing_data: list[str]

    def to_prompt_snippets(self, *, limit: int = 12) -> list[str]:
        snippets: list[str] = [
            f"[{self.codename}] enrichment_depth={self.depth} score={self.score:.2f}",
        ]
        snippets.extend(f"[Hook] {item}" for item in self.personalization_hooks[:4])
        snippets.extend(f"[Angle] {item}" for item in self.value_angles[:4])
        snippets.extend(f"[Evidence] {item}" for item in self.evidence_highlights[:4])
        snippets.extend(f"[Missing] {item}" for item in self.missing_data[:3])

        out: list[str] = []
        seen: set[str] = set()
        for item in snippets:
            value = " ".join(item.split()).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value[:220])
            if len(out) >= max(1, limit):
                break
        return out

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
    selected_site_snippet = ""

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
            selected_site_snippet = " ".join(str(selected_site.snippet or "").split()).strip()

    site_summary = ""
    evidence: list[str] = []
    pain_hypotheses = _infer_pains(company)
    opportunity_hypotheses = _infer_opportunities(company)
    if selected_site_snippet:
        site_summary = selected_site_snippet[:900]
        evidence.append("Utilizzato estratto testuale da Search API come base")

    if website:
        try:
            snapshot = await fetch_website_snapshot(
                url=website,
                headless=headless,
                timeout_ms=snapshot_timeout_ms,
            )
            site_summary = _merge_site_summary(
                site_summary,
                snapshot.text_excerpt[:1200],
                max_chars=2200,
            )
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
                    site_summary = _merge_site_summary(
                        site_summary,
                        extra_snapshot.text_excerpt[:500],
                        max_chars=2200,
                    )
                except Exception:
                    continue
        except Exception:
            evidence.append("Sito non analizzabile in modo completo")

    if not news_items:
        selected_site = SearchHit(title=company.company_name, url=website) if website else None
        _, news_items = collect_company_news(
            company_name=company.company_name,
            city=city,
            selected_site=selected_site,
            news_max_results=6,
        )

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


def run_nebula_enrichment_machine(
    *,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
) -> NebulaEnrichmentMachine:
    codename = "NebulaForge"
    score = _enrichment_score(company=company, contact=contact, dossier=dossier)
    depth = "high" if score >= 0.70 else ("medium" if score >= 0.45 else "low")

    hooks = _personalization_hooks(company=company, contact=contact, dossier=dossier)
    angles = _value_angles(dossier=dossier)
    evidence = _evidence_highlights(dossier=dossier)
    missing = _missing_data_flags(company=company, contact=contact, dossier=dossier)

    return NebulaEnrichmentMachine(
        codename=codename,
        score=score,
        depth=depth,
        personalization_hooks=hooks,
        value_angles=angles,
        evidence_highlights=evidence,
        missing_data=missing,
    )


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
    if limit <= 0:
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


def _merge_site_summary(base: str, extra: str, *, max_chars: int) -> str:
    base_clean = " ".join((base or "").split()).strip()
    extra_clean = " ".join((extra or "").split()).strip()
    if not extra_clean:
        return base_clean[:max_chars]
    if not base_clean:
        return extra_clean[:max_chars]
    if extra_clean in base_clean:
        return base_clean[:max_chars]
    if base_clean in extra_clean:
        return extra_clean[:max_chars]
    merged = f"{base_clean}\n{extra_clean}"
    return merged[:max_chars]


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


def _enrichment_score(*, company: LeadCompany, contact: LeadContact | None, dossier: EnrichmentDossier) -> float:
    score = 0.0
    if company.industry:
        score += 0.15
    if company.location:
        score += 0.10
    if company.keywords:
        score += 0.10
    if contact and contact.title:
        score += 0.10
    if contact and contact.seniority:
        score += 0.05

    site_summary_len = len((dossier.site_summary or "").strip())
    if site_summary_len >= 220:
        score += 0.20
    elif site_summary_len >= 80:
        score += 0.12

    score += min(0.12, len(dossier.news_items) * 0.04)
    score += min(0.10, len(dossier.evidence) * 0.025)
    score += min(0.12, len(dossier.sources) * 0.03)

    if company.linkedin_company or (contact and contact.linkedin_person):
        score += 0.06

    return round(min(score, 1.0), 2)


def _personalization_hooks(
    *,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
) -> list[str]:
    hooks: list[str] = []
    if contact and contact.full_name:
        first_name = contact.full_name.split()[0].strip()
        if first_name:
            hooks.append(f"Referente: {first_name}")
    if contact and contact.title:
        hooks.append(f"Ruolo target: {contact.title}")
    if company.industry:
        hooks.append(f"Settore: {company.industry}")
    if company.location:
        hooks.append(f"Presidio geografico: {company.location}")
    if company.keywords:
        hooks.append(f"Keyword azienda: {company.keywords}")
    if dossier.news_items:
        event_title = _first_event_news_title(dossier.news_items)
        if event_title:
            hooks.append(f"Evento recente: {event_title}")
        else:
            hooks.append(f"News recente: {dossier.news_items[0].title}")
    if company.linkedin_company:
        hooks.append(f"LinkedIn aziendale: {company.linkedin_company}")

    return _dedupe_keep_order(hooks, limit=8)


def _value_angles(*, dossier: EnrichmentDossier) -> list[str]:
    angles: list[str] = []
    for pain in dossier.pain_hypotheses[:3]:
        value = str(pain).strip()
        if value:
            angles.append(f"Pain: {value}")
    for opportunity in dossier.opportunity_hypotheses[:3]:
        value = str(opportunity).strip()
        if value:
            angles.append(f"Opportunity: {value}")
    return _dedupe_keep_order(angles, limit=8)


def _evidence_highlights(*, dossier: EnrichmentDossier) -> list[str]:
    highlights: list[str] = []
    highlights.extend(str(item).strip() for item in dossier.evidence[:6])
    highlights.extend(f"Fonte: {item}" for item in dossier.sources[:4])
    return _dedupe_keep_order(highlights, limit=10)


def _missing_data_flags(
    *,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
) -> list[str]:
    flags: list[str] = []
    if not company.website:
        flags.append("missing_company_website")
    if not company.industry:
        flags.append("missing_company_industry")
    if not (contact and contact.title):
        flags.append("missing_contact_title")
    if len((dossier.site_summary or "").strip()) < 80:
        flags.append("missing_site_summary_depth")
    if not dossier.news_items:
        flags.append("missing_news_context")
    elif not any(is_event_news_hit(item) for item in dossier.news_items):
        flags.append("missing_company_event_news")
    if not dossier.sources:
        flags.append("missing_verified_sources")
    return flags


def _first_event_news_title(news_items: list[SearchHit]) -> str:
    for item in news_items:
        if is_event_news_hit(item):
            title = str(item.title or "").strip()
            if title:
                return title
    return ""


def _dedupe_keep_order(items: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = " ".join(str(item).split()).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= max(1, limit):
            break
    return out
