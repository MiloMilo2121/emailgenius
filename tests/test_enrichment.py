from __future__ import annotations

import unittest

from emailgenius.enrichment import run_nebula_enrichment_machine
from emailgenius.types import EnrichmentDossier, LeadCompany, LeadContact, SearchHit


class NebulaEnrichmentMachineTests(unittest.TestCase):
    def test_high_signal_profile_builds_rich_snippets(self) -> None:
        company = LeadCompany(
            company_key="acme",
            company_name="Acme SRL",
            website="https://acme.example",
            linkedin_company="https://www.linkedin.com/company/acme",
            industry="machinery",
            employee_count=80,
            location="Vicenza, Veneto, Italy",
            keywords="automation, sustainability",
            tech="ERP, CRM",
            founded_year=2001,
        )
        contact = LeadContact(
            full_name="Mario Rossi",
            title="Operations Director",
            seniority="director",
            email="mario@example.com",
            linkedin_person="https://www.linkedin.com/in/mario-rossi",
            quality_flag="good",
            score=78.0,
        )
        dossier = EnrichmentDossier(
            site_summary="Azienda specializzata in automazione industriale con focus su efficienza energetica e linea produttiva.",
            news_items=[SearchHit(title="Acme investe su nuovi impianti", url="https://news.example/acme-1")],
            pain_hypotheses=["pressione su costi energetici"],
            opportunity_hypotheses=["quick win su processi ad alto consumo"],
            evidence=["Homepage title: Acme SRL"],
            sources=["https://acme.example", "https://news.example/acme-1"],
        )

        nebula = run_nebula_enrichment_machine(company=company, contact=contact, dossier=dossier)
        snippets = nebula.to_prompt_snippets(limit=12)

        self.assertIn(nebula.depth, {"medium", "high"})
        self.assertGreaterEqual(nebula.score, 0.45)
        self.assertTrue(any("NebulaForge" in item for item in snippets))
        self.assertTrue(any(item.startswith("[Hook]") for item in snippets))
        self.assertTrue(any("Evento recente:" in item for item in nebula.personalization_hooks))

    def test_low_signal_profile_marks_missing_data(self) -> None:
        company = LeadCompany(
            company_key="void",
            company_name="Void SRL",
            website=None,
            linkedin_company=None,
            industry=None,
            employee_count=None,
            location=None,
            keywords=None,
            tech=None,
            founded_year=None,
        )
        dossier = EnrichmentDossier(
            site_summary="",
            pain_hypotheses=[],
            opportunity_hypotheses=[],
            evidence=[],
            sources=[],
        )

        nebula = run_nebula_enrichment_machine(company=company, contact=None, dossier=dossier)

        self.assertEqual(nebula.depth, "low")
        self.assertIn("missing_company_website", nebula.missing_data)
        self.assertIn("missing_news_context", nebula.missing_data)


if __name__ == "__main__":
    unittest.main()
