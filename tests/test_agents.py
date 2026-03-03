from __future__ import annotations

import unittest

from emailgenius.agents import CampaignAgentEngine
from emailgenius.types import EnrichmentDossier, LeadCompany, LeadContact, ParentProfile, SearchHit


class _FakeLLM:
    def _call_chat_json(self, *, system_prompt: str, user_prompt: str):
        if "EMAIL 1" in system_prompt:
            return {
                "subject": "Nuovo trigger operativo",
                "body": "Ciao Mario, se stai per firmare un ordine, fermati 24 ore.",
                "goal": "Aprire conversazione",
            }
        return {
            "steps": [
                {
                    "step_id": "E2",
                    "subject": "Case study numerico",
                    "body": "Un cliente ha ridotto tempi del 22%.",
                    "goal": "Credibilita",
                },
                {
                    "step_id": "E3",
                    "subject": "Priorita trimestrale",
                    "body": "Qual e la priorita ora?",
                    "goal": "Qualifica",
                },
                {
                    "step_id": "BREAKUP",
                    "subject": "Chiudo qui",
                    "body": "Se non e il momento, nessun problema.",
                    "goal": "Chiusura",
                },
            ]
        }


class _NoChatLLM:
    pass


class AgentEngineTests(unittest.TestCase):
    def _parent(self) -> ParentProfile:
        return ParentProfile(
            slug="azienda-a",
            company_name="Azienda A",
            tone="formale-consulenziale",
            no_go_claims=["garantito"],
            sender_name="Ivan",
            sender_company="Contributo Facile",
            outreach_seed_template="Ciao {{first_name}}, proposta per {{company_name}}.",
        )

    def _company(self) -> LeadCompany:
        return LeadCompany(
            company_key="acme",
            company_name="Acme",
            website="https://acme.example",
            linkedin_company=None,
            industry="manifattura",
            employee_count=120,
            location="Brescia",
            keywords="cnc",
            tech=None,
            founded_year=1998,
        )

    def _contact(self) -> LeadContact:
        return LeadContact(
            full_name="Mario Rossi",
            title="CEO",
            seniority="c_suite",
            email="mario@example.com",
            linkedin_person=None,
            quality_flag="good",
            score=92.0,
        )

    def test_generate_sequence_returns_four_steps(self) -> None:
        engine = CampaignAgentEngine(llm=_FakeLLM())
        dossier = EnrichmentDossier(
            site_summary="sito aziendale",
            news_items=[SearchHit(title="Acme apre un nuovo impianto", url="https://example.com/news")],
            pain_hypotheses=["pressione su tempi di avvio"],
            opportunity_hypotheses=["riduzione rischio operativo"],
        )

        sequence = engine.generate_sequence(
            parent=self._parent(),
            company=self._company(),
            contact=self._contact(),
            dossier=dossier,
            marketing_snippets=["snippet numerico"],
        )

        self.assertTrue(sequence.attack_angle)
        self.assertEqual([step.step_id for step in sequence.steps], ["E1", "E2", "E3", "BREAKUP"])

    def test_compliance_guard_flags_risky_claims(self) -> None:
        class _RiskyLLM(_FakeLLM):
            def _call_chat_json(self, *, system_prompt: str, user_prompt: str):
                if "EMAIL 1" in system_prompt:
                    return {
                        "subject": "Risultato garantito",
                        "body": "Ti garantiamo risultato immediato.",
                        "goal": "Aprire conversazione",
                    }
                return super()._call_chat_json(system_prompt=system_prompt, user_prompt=user_prompt)

        engine = CampaignAgentEngine(llm=_RiskyLLM(), max_compliance_retries=1)
        dossier = EnrichmentDossier(site_summary="base")
        sequence = engine.generate_sequence(
            parent=self._parent(),
            company=self._company(),
            contact=self._contact(),
            dossier=dossier,
            marketing_snippets=[],
        )

        flags = sequence.global_risk_flags
        self.assertTrue(any(flag.startswith("claim_") or flag.startswith("no_go:") for flag in flags))

    def test_strict_mode_raises_when_llm_chat_unavailable(self) -> None:
        engine = CampaignAgentEngine(llm=_NoChatLLM())  # type: ignore[arg-type]
        dossier = EnrichmentDossier(site_summary="base")
        with self.assertRaises(RuntimeError):
            engine.generate_sequence(
                parent=self._parent(),
                company=self._company(),
                contact=self._contact(),
                dossier=dossier,
                marketing_snippets=[],
                llm_policy="strict",
            )

    def test_fallback_mode_survives_when_llm_chat_unavailable(self) -> None:
        engine = CampaignAgentEngine(llm=_NoChatLLM())  # type: ignore[arg-type]
        dossier = EnrichmentDossier(site_summary="base")
        sequence = engine.generate_sequence(
            parent=self._parent(),
            company=self._company(),
            contact=self._contact(),
            dossier=dossier,
            marketing_snippets=[],
            llm_policy="fallback",
        )
        self.assertEqual([step.step_id for step in sequence.steps], ["E1", "E2", "E3", "BREAKUP"])


if __name__ == "__main__":
    unittest.main()
