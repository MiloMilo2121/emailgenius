from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emailgenius.campaign import run_campaign
from emailgenius.config import AppConfig
from emailgenius.gdrive import DriveLeadRow, ExportResult, SyncReport
from emailgenius.types import EnrichmentDossier, ParentProfile, SequenceResult, SequenceStep


class _FakeStore:
    def __init__(self, profile: ParentProfile) -> None:
        self.profile = profile
        self.inserted = []
        self.summary = None
        self._seen: set[str] = set()

    def get_parent_profile(self, slug: str):
        return self.profile if slug == self.profile.slug else None

    def get_active_parent_slug(self):
        return self.profile.slug

    def list_parent_profiles(self):
        return [self.profile]

    def create_campaign(self, *, parent_slug: str, leads_file: str, sheet_id: str | None) -> str:
        return "campaign-drive"

    def begin_drive_row_ingestion(
        self,
        *,
        idempotency_key: str,
        sheet_id: str,
        tab_name: str,
        row_index: int,
        modified_time: str,
        campaign_id: str,
    ) -> bool:
        if idempotency_key in self._seen:
            return False
        self._seen.add(idempotency_key)
        return True

    def complete_drive_row_ingestion(self, *, idempotency_key: str, record_id: str | None, status: str, error_message: str | None = None):
        return None

    def search_knowledge_chunks(self, *, parent_slug: str, kind: str, query_embedding, top_k: int = 6):
        return []

    def insert_campaign_company_result(self, result, *, extra_payload=None):
        self.inserted.append((result, extra_payload))
        return "record-drive-1"

    def finalize_campaign(self, campaign_id: str, summary):
        self.summary = summary

    def purge_expired_campaign_data(self, retention_days: int):
        return 0


class _FakeLLM:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return []


class _FakeEngine:
    def __init__(self, *, llm, max_compliance_retries: int = 2, checkpointer=None) -> None:
        pass

    def generate_sequence(self, *, parent, company, contact, dossier, marketing_snippets, llm_policy: str = "strict") -> SequenceResult:
        return SequenceResult(
            attack_angle="trigger",
            trigger_facts=["news"],
            steps=[
                SequenceStep(step_id="E1", subject="sub1", body="body1", goal="g1"),
                SequenceStep(step_id="E2", subject="sub2", body="body2", goal="g2"),
                SequenceStep(step_id="E3", subject="sub3", body="body3", goal="g3"),
                SequenceStep(step_id="BREAKUP", subject="sub4", body="body4", goal="g4"),
            ],
        )


class CampaignDriveTests(unittest.TestCase):
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
            outreach_seed_template="Ciao {{first_name}}, proposta per {{company_name}}.",
        )

    def _config(self) -> AppConfig:
        return AppConfig(
            database_url="postgresql://local",
            openai_api_key=None,
            openai_base_url=None,
            openai_chat_model="gpt-5",
            openai_embedding_model="text-embedding-3-small",
            google_service_account_json=None,
            retention_days=90,
        )

    def test_drive_mode_processes_rows_and_persists_sequence(self) -> None:
        profile = self._profile()
        store = _FakeStore(profile)
        llm = _FakeLLM()

        lead_row = DriveLeadRow(
            raw_row={"companyName": "Beta SRL", "Email": "anna@example.com", "First Name": "Anna", "Last Name": "Verdi"},
            canonical_row={
                "Company Name": "Beta SRL",
                "Cleaned Company Name": "Beta SRL",
                "Company Website Full": "",
                "Email": "anna@example.com",
                "First Name": "Anna",
                "Last Name": "Verdi",
                "Full Name": "Anna Verdi",
                "parent_slug": "azienda-a",
            },
            sheet_id="sheet-1",
            sheet_name="Leads",
            tab_name="Sheet1",
            row_index=2,
            modified_time="2026-03-03T10:00:00Z",
            idempotency_key="key-1",
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "emailgenius.campaign.build_workspace_clients",
            return_value=type("C", (), {"drive": object(), "docs": object(), "sheets": object()})(),
        ), patch(
            "emailgenius.campaign.sync_parent_profiles", return_value=SyncReport(synced=1)
        ), patch(
            "emailgenius.campaign.sync_knowledge_base", return_value=SyncReport(synced=1)
        ), patch(
            "emailgenius.campaign.fetch_leads_sheet", return_value=[lead_row]
        ), patch(
            "emailgenius.campaign.CampaignAgentEngine", _FakeEngine
        ), patch(
            "emailgenius.campaign.export_sequence_to_drive",
            return_value=ExportResult(docs_created=1, status_rows_written=1, doc_urls=["https://docs.example/1"]),
        ), patch(
            "emailgenius.campaign.run_nebula_enrichment_machine",
            return_value=type("Nebula", (), {"to_prompt_snippets": lambda self, limit=10: ["n1"]})(),
        ):
            summary, export_path, rows = run_campaign(
                config=self._config(),
                store=store,
                llm=llm,
                parent_slug="azienda-a",
                leads_csv_path=None,
                out_dir=str(Path(tmpdir) / "out"),
                sheet_id=None,
                io_mode="drive",
                workspace_folder_id="workspace-1",
                llm_policy="fallback",
            )
            self.assertTrue(export_path.exists())

        self.assertEqual(summary.io_mode, "drive")
        self.assertEqual(summary.rows_generated_ok, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(store.inserted), 1)
        payload = store.inserted[0][1]
        self.assertEqual(payload.get("idempotency_key"), "key-1")


if __name__ == "__main__":
    unittest.main()
