from __future__ import annotations

import unittest

from emailgenius.llm import LLMGateway
from emailgenius.types import EnrichmentDossier, LeadCompany, LeadContact, ParentProfile


class LLMFallbackTests(unittest.TestCase):
    def test_strict_mode_without_api_key_raises(self) -> None:
        llm = LLMGateway(api_key=None, chat_model="gpt-5", embedding_model="text-embedding-3-small")

        parent = ParentProfile(
            slug="azienda-a",
            company_name="Azienda A",
            tone="formale-consulenziale",
            offer_catalog=["Servizio 1"],
            icp=["PMI manifatturiere"],
            proof_points=["Case study"],
            objections=["budget"],
            cta_policy="call conoscitiva 20-30 min",
            no_go_claims=["garantito"],
            compliance_notes=["uso dati pubblici"],
        )
        company = LeadCompany(
            company_key="acme",
            company_name="Acme",
            website="https://acme.it",
            linkedin_company=None,
            industry="machinery",
            employee_count=50,
            location="Bergamo, Lombardy, Italy",
            keywords="automation, b2b",
            tech="WordPress",
            founded_year=1999,
        )
        contact = LeadContact(
            full_name="Mario Rossi",
            title="CEO",
            seniority="c_suite",
            email="mario@example.com",
            linkedin_person=None,
            quality_flag="good",
            score=80,
        )
        dossier = EnrichmentDossier(
            site_summary="azienda manifatturiera",
            pain_hypotheses=["pressione su efficienza"],
            opportunity_hypotheses=["quick win commerciali"],
        )

        with self.assertRaises(RuntimeError):
            llm.generate_campaign_variants(
                parent=parent,
                company=company,
                contact=contact,
                dossier=dossier,
                marketing_snippets=[],
            )

    def test_fallback_mode_generates_ab_variants_without_api_key(self) -> None:
        llm = LLMGateway(api_key=None, chat_model="gpt-5", embedding_model="text-embedding-3-small")
        parent = ParentProfile(
            slug="azienda-a",
            company_name="Azienda A",
            tone="formale-consulenziale",
            offer_catalog=["Servizio 1"],
            icp=["PMI manifatturiere"],
            proof_points=["Case study"],
            objections=["budget"],
            cta_policy="call conoscitiva 20-30 min",
            no_go_claims=["garantito"],
            compliance_notes=["uso dati pubblici"],
            sender_name="Ivan Lorenzoni",
            sender_company="Contributo Facile",
            outreach_seed_template="Ciao {{first_name}}, opportunita per {{company_name}}. {{sender_name}}",
        )
        company = LeadCompany(
            company_key="acme",
            company_name="Acme",
            website="https://acme.it",
            linkedin_company=None,
            industry="machinery",
            employee_count=50,
            location="Bergamo, Lombardy, Italy",
            keywords="automation, b2b",
            tech="WordPress",
            founded_year=1999,
        )
        contact = LeadContact(
            full_name="Mario Rossi",
            title="CEO",
            seniority="c_suite",
            email="mario@example.com",
            linkedin_person=None,
            quality_flag="good",
            score=80,
        )
        dossier = EnrichmentDossier(
            site_summary="azienda manifatturiera",
            pain_hypotheses=["pressione su efficienza"],
            opportunity_hypotheses=["quick win commerciali"],
        )

        variants, recommended, flags = llm.generate_campaign_variants(
            parent=parent,
            company=company,
            contact=contact,
            dossier=dossier,
            marketing_snippets=[],
            llm_policy="fallback",
            variant_mode="ab",
        )

        self.assertEqual(len(variants), 2)
        self.assertIn(recommended, {"A", "B"})
        self.assertIsInstance(flags, list)


if __name__ == "__main__":
    unittest.main()
