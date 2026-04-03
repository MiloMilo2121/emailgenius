from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emailgenius.campaign import export_instantly_campaign, run_campaign
from emailgenius.config import AppConfig
from emailgenius.types import (
    InstantlyDraft,
    ParentProfile,
    ResearchDossier,
    ResearchSource,
)


class _ResearchStore:
    def __init__(self, profile: ParentProfile) -> None:
        self.profile = profile
        self.inserted = []
        self.summary = None

    def get_parent_profile(self, slug: str):
        return self.profile if slug == self.profile.slug else None

    def create_campaign(self, *, parent_slug: str, leads_file: str, sheet_id: str | None) -> str:
        return "campaign-research"

    def insert_campaign_company_result(self, result, *, extra_payload=None):
        self.inserted.append((result, extra_payload))
        return "record-research-1"

    def finalize_campaign(self, campaign_id: str, summary):
        self.summary = summary

    def purge_expired_campaign_data(self, retention_days: int):
        return 0

    def search_knowledge_chunks(self, *, parent_slug: str, kind: str, query_embedding, top_k: int = 6):
        return []

    def list_campaign_records(self, campaign_id: str):
        payload = self.inserted[0][1] | {
            "instantly_draft": {
                "subject_line": "Subject Research",
                "personalization": "Ho visto il nuovo impianto e credo sia il momento giusto.",
            }
        }
        return [
            {
                "parent_slug": self.profile.slug,
                "company_name": "Beta SRL",
                "contact_name": "Anna Verdi",
                "contact_email": "anna@example.com",
                "payload_json": payload,
            }
        ]


class _ResearchLLM:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return []

    def generate_research_dossier(self, *, parent, company, contact, research_bundle, llm_policy: str = "strict"):
        return ResearchDossier(
            company_name=company.company_name,
            domain="beta.it",
            company_summary="Beta SRL sta espandendo la sua capacita' produttiva.",
            trigger_event="Nuovo impianto a Brescia",
            pain_hypothesis="serve un messaggio commerciale piu' allineato alla fase di espansione",
            personalization_angle="state muovendo investimenti reali e ha senso attivare outreach piu' chirurgico",
            key_facts=["Nuovo impianto a Brescia", "settore manufacturing"],
            recent_news=[ResearchSource(title="Nuovo impianto a Brescia", url="https://example.com/news")],
            citations=["https://example.com/news"],
            research_sources=list(research_bundle.get("selected_sources") or []),
            confidence=0.84,
        )

    def generate_instantly_draft(self, *, parent, company, contact, research_dossier, llm_policy: str = "strict"):
        return InstantlyDraft(
            subject_line="Subject Research",
            personalization="Ho visto il nuovo impianto a Brescia e credo sia il momento giusto per aprire un confronto molto concreto.",
            intro_line="Ciao Anna,",
            cta_line="Se ha senso, ti mando due spunti concreti.",
            body_template="Ciao {{FirstName}},\n\n{{Personalization}}\n\nSe ha senso, ti mando due spunti concreti.",
            confidence=0.88,
        )


class CampaignResearchTests(unittest.TestCase):
    def _profile(self) -> ParentProfile:
        return ParentProfile(
            slug="azienda-a",
            company_name="Azienda A",
            tone="formale-consulenziale",
            offer_catalog=["Servizio 1"],
            icp=["PMI"],
            proof_points=["case"],
            objections=["budget"],
            cta_policy="call conoscitiva 20-30 min",
            no_go_claims=["garantito"],
            compliance_notes=["dati pubblici"],
            sender_name="Ivan",
            sender_company="Contributo Facile",
            outreach_seed_template="Ciao {{first_name}}",
            instantly_intro_template="Ciao {{first_name}},",
            instantly_cta_template="Se ha senso, ti mando due spunti concreti.",
        )

    def _config(self) -> AppConfig:
        return AppConfig(
            database_url="postgresql://local",
            openai_api_key="or-key",
            openai_base_url="https://openrouter.ai/api/v1",
            openai_chat_model="deepseek/deepseek-chat-v3.1",
            openai_embedding_model="hash-local",
            google_service_account_json=None,
            retention_days=90,
            exa_api_key="exa-key",
            research_model="deepseek/deepseek-chat-v3.1",
            writer_model="anthropic/claude-3.5-haiku",
        )

    def test_local_campaign_uses_research_pipeline_when_exa_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "emailgenius.campaign.ExaClient.collect_company_research",
            return_value={
                "company_name": "Beta SRL",
                "selected_sources": ["web", "instagram"],
                "official_pages": {"results": []},
                "news_results": {"results": []},
            },
        ) as mocked_collect:
            leads_path = Path(tmpdir) / "leads.csv"
            with leads_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["Email", "First Name", "Last Name", "companyName", "website", "jobTitle"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "Email": "anna@example.com",
                        "First Name": "Anna",
                        "Last Name": "Verdi",
                        "companyName": "Beta SRL",
                        "website": "https://beta.it",
                        "jobTitle": "Founder",
                    }
                )

            store = _ResearchStore(self._profile())
            llm = _ResearchLLM()
            summary, export_path, rows = run_campaign(
                config=self._config(),
                store=store,
                llm=llm,  # type: ignore[arg-type]
                parent_slug="azienda-a",
                leads_csv_path=str(leads_path),
                out_dir=str(Path(tmpdir) / "out"),
                sheet_id=None,
                recipient_mode="row",
                variant_mode="ab",
                output_schema="ab",
                llm_policy="strict",
                enrichment_mode="auto",
                max_concurrency=1,
                max_retries=1,
                backoff_base_seconds=0.0,
                research_sources=["web", "instagram"],
            )

            self.assertEqual(summary.rows_generated_ok, 1)
            self.assertEqual(summary.research_sources, ["web", "instagram"])
            self.assertTrue(export_path.exists())
            self.assertEqual(rows[0].get("SubjectLine"), "Subject Research")
            self.assertIn("nuovo impianto", str(rows[0].get("Personalization") or "").lower())
            self.assertEqual(rows[0].get("ResearchSources"), "web; instagram")
            result = store.inserted[0][0]
            self.assertIsNotNone(result.research_dossier)
            self.assertIsNotNone(result.instantly_draft)
            self.assertEqual(result.research_dossier.research_sources, ["web", "instagram"])
            self.assertEqual(store.inserted[0][1]["research_sources"], ["web", "instagram"])
            self.assertEqual(mocked_collect.call_args.kwargs["research_sources"], ["web", "instagram"])

    def test_export_instantly_campaign_writes_expected_columns(self) -> None:
        store = _ResearchStore(self._profile())
        store.inserted.append(
            (
                None,
                {
                    "raw_row": {"Email": "anna@example.com"},
                    "instantly_draft": {
                        "subject_line": "Subject Research",
                        "personalization": "Ho visto il nuovo impianto e credo sia il momento giusto.",
                    },
                },
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "instantly.csv"
            export_instantly_campaign(store, "campaign-research", str(target))
            with target.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Email"], "anna@example.com")
            self.assertEqual(rows[0]["SubjectLine"], "Subject Research")


if __name__ == "__main__":
    unittest.main()
