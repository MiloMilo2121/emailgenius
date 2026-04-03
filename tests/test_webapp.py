from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from emailgenius.config import AppConfig
from emailgenius.types import ParentProfile
from emailgenius.webapp import create_app


class _FakeStore:
    def __init__(self) -> None:
        self.parents: dict[str, ParentProfile] = {
            "azienda-a": ParentProfile(
                slug="azienda-a",
                company_name="Azienda A",
                tone="formale-consulenziale",
                offer_catalog=["Audit"],
                icp=["PMI"],
                proof_points=["Case"],
                objections=["Budget"],
                cta_policy="call conoscitiva 20-30 min",
                no_go_claims=["garantito"],
                compliance_notes=["dati pubblici"],
                sender_name="Ivan",
                sender_company="Contributo Facile",
                outreach_seed_template="Ciao {{first_name}}",
            )
        }
        self.active_slug = "azienda-a"

    def get_active_parent_slug(self):
        return self.active_slug

    def list_parent_profiles(self):
        return list(self.parents.values())

    def upsert_parent_profile(self, profile: ParentProfile, *, set_active: bool = False) -> None:
        self.parents[profile.slug] = profile
        if set_active:
            self.active_slug = profile.slug

    def get_parent_profile(self, slug: str):
        return self.parents.get(slug)

    def set_active_parent(self, slug: str) -> None:
        self.active_slug = slug

    def list_knowledge_documents(self, slug: str):
        return [{"kind": "marketing", "source_path": "deck.pdf", "created_at": "2026-04-02T10:00:00Z"}]

    def list_campaign_summaries(self, *, limit: int = 20, parent_slug: str | None = None):
        rows = [
            {
                "id": "camp-1",
                "parent_slug": "azienda-a",
                "leads_file": "leads.csv",
                "sheet_id": None,
                "status": "COMPLETED",
                "started_at": "2026-04-02T10:00:00Z",
                "finished_at": "2026-04-02T10:10:00Z",
                "summary_json": {"io_mode": "local", "rows_generated_ok": 3, "actual_cost_eur": 0.15},
            }
        ]
        if parent_slug:
            return [row for row in rows if row["parent_slug"] == parent_slug][:limit]
        return rows[:limit]

    def get_campaign_summary(self, campaign_id: str):
        if campaign_id != "camp-1":
            return None
        return {
            "id": "camp-1",
            "parent_slug": "azienda-a",
            "leads_file": "leads.csv",
            "sheet_id": None,
            "status": "COMPLETED",
            "started_at": "2026-04-02T10:00:00Z",
            "finished_at": "2026-04-02T10:10:00Z",
            "summary_json": {"io_mode": "local", "rows_generated_ok": 3, "rows_failed": 0, "actual_cost_eur": 0.15},
        }

    def list_campaign_records(self, campaign_id: str):
        return [
            {
                "id": "record-1",
                "parent_slug": "azienda-a",
                "company_name": "Beta SRL",
                "company_key": "beta-srl",
                "contact_name": "Anna Verdi",
                "contact_title": "Owner",
                "contact_email": "anna@example.com",
                "status": "PENDING",
                "payload_json": {
                    "final_subject": "Subject A",
                    "final_body": "Body A",
                    "company": {"company_name": "Beta SRL", "industry": "manufacturing", "location": "Brescia"},
                    "contact": {"full_name": "Anna Verdi", "email": "anna@example.com"},
                    "dossier": {
                        "site_summary": "Beta SRL produce componenti meccanici.",
                        "news_items": [{"title": "Nuovo stabilimento a Brescia", "url": "https://example.com/news"}],
                        "evidence": ["Homepage title: Beta SRL"],
                        "sources": ["https://example.com"],
                    },
                    "sequence_result": {
                        "attack_angle": "nuovo stabilimento",
                        "trigger_facts": ["Nuovo stabilimento a Brescia"],
                        "steps": [
                            {"step_id": "E1", "subject": "Subject A", "body": "Body A"},
                        ],
                    },
                },
            }
        ]


class _FakeLLM:
    pass


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeStore()
        self.app = create_app(
            config=AppConfig(
                database_url="postgresql://local",
                openai_api_key=None,
                openai_base_url=None,
                openai_chat_model="gpt-5",
                openai_embedding_model="text-embedding-3-small",
                google_service_account_json=None,
                retention_days=90,
                workspace_folder_id="workspace-1",
            ),
            store_factory=lambda: self.store,
            llm_factory=lambda: _FakeLLM(),  # type: ignore[return-value]
        )
        self.client = TestClient(self.app)

    def test_dashboard_renders_registered_parent(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Azienda A", response.text)
        self.assertIn("Genera email senza passare dal terminale", response.text)
        self.assertIn('name="research_sources"', response.text)
        self.assertIn("Instagram", response.text)
        self.assertIn("LinkedIn", response.text)

    def test_local_campaign_form_queues_research_sources(self) -> None:
        with patch("emailgenius.webapp._launch_background_job") as mocked_launch:
            response = self.client.post(
                "/campaigns/local",
                data={
                    "slug": "azienda-a",
                    "llm_policy": "strict",
                    "cost_cap_eur": "12.5",
                    "research_sources": ["web", "instagram"],
                },
                files={"leads_file": ("leads.csv", "Email,First Name,companyName\nanna@example.com,Anna,Beta SRL\n", "text/csv")},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/jobs/"))
        _, kwargs = mocked_launch.call_args
        self.assertEqual(kwargs["research_sources"], ["web", "instagram"])
        job = self.app.state.jobs.list_recent(limit=1)[0]
        self.assertEqual(job.payload_json["research_sources"], ["web", "instagram"])

    def test_parent_save_redirects_to_detail(self) -> None:
        response = self.client.post(
            "/parents",
            data={
                "slug": "azienda-b",
                "company_name": "Azienda B",
                "tone": "diretto",
                "offer_catalog": "Servizio 1",
                "icp": "PMI",
                "proof_points": "Case",
                "objections": "Budget",
                "cta_policy": "call",
                "no_go_claims": "garantito",
                "compliance_notes": "pubblico",
                "sender_name": "Luca",
                "sender_company": "Brand B",
                "sender_phone": "",
                "sender_booking_url": "",
                "outreach_seed_template": "Ciao {{first_name}}",
                "set_active": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/parents/azienda-b?message=Parent+salvato")
        self.assertIn("azienda-b", self.store.parents)

    def test_campaign_detail_shows_final_subject_and_body(self) -> None:
        response = self.client.get("/campaigns/camp-1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Subject A", response.text)
        self.assertIn("Body A", response.text)
        self.assertIn("Instantly export", response.text)
        self.assertIn("{{Personalization}}", response.text)

    def test_campaign_instantly_csv_download(self) -> None:
        response = self.client.get("/campaigns/camp-1/export/instantly.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"campaign-camp-1-instantly.csv\"", response.headers["content-disposition"])
        csv_text = response.text
        self.assertIn("Email,FirstName,LastName,CompanyName,SubjectLine,Personalization,CampaignId,ParentSlug", csv_text)
        self.assertIn("anna@example.com", csv_text)
        self.assertIn("Beta SRL", csv_text)
        self.assertIn("Subject A", csv_text)
        self.assertIn("Nuovo stabilimento a Brescia", csv_text)

    def test_job_detail_returns_json_when_requested(self) -> None:
        job = self.app.state.jobs.create(
            label="Sync Drive azienda-a",
            kind="workspace_sync",
            parent_slug="azienda-a",
            payload_json={"workspace_folder_id": "workspace-1"},
        )
        response = self.client.get(f"/jobs/{job.job_id}", headers={"accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["job_id"], job.job_id)
        self.assertEqual(payload["label"], "Sync Drive azienda-a")

    def test_reports_static_are_served_from_emailgenius_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"EMAILGENIUS_HOME": tmpdir}):
            reports_dir = Path(tmpdir) / "web-reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / "sample.txt").write_text("report ok", encoding="utf-8")

            app = create_app(
                config=AppConfig(
                    database_url="postgresql://local",
                    openai_api_key=None,
                    openai_base_url=None,
                    openai_chat_model="gpt-5",
                    openai_embedding_model="text-embedding-3-small",
                    google_service_account_json=None,
                    retention_days=90,
                ),
                store_factory=lambda: self.store,
                llm_factory=lambda: _FakeLLM(),  # type: ignore[return-value]
            )
            client = TestClient(app)

            response = client.get("/reports/sample.txt")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "report ok")


if __name__ == "__main__":
    unittest.main()
