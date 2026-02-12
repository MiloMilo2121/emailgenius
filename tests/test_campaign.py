from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from emailgenius.campaign import export_campaign, run_campaign
from emailgenius.config import AppConfig
from emailgenius.types import DraftEmailVariant, EnrichmentDossier, ParentProfile


class _FakeStore:
    def __init__(self, profile: ParentProfile) -> None:
        self.profile = profile
        self.inserted = []
        self.summary = None

    def get_parent_profile(self, slug: str):
        return self.profile if slug == self.profile.slug else None

    def create_campaign(self, *, parent_slug: str, leads_file: str, sheet_id: str | None) -> str:
        return "campaign-test"

    def search_knowledge_chunks(self, *, parent_slug: str, kind: str, query_embedding, top_k: int = 6):
        return []

    def insert_campaign_company_result(self, result, *, extra_payload=None):
        self.inserted.append((result, extra_payload))
        return "record-1"

    def finalize_campaign(self, campaign_id: str, summary):
        self.summary = summary

    def purge_expired_campaign_data(self, retention_days: int):
        return 0


class _FakeLLM:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return []

    def generate_campaign_variants(
        self,
        *,
        parent,
        company,
        contact,
        dossier,
        marketing_snippets,
        variant_mode: str = "ab",
        llm_policy: str = "strict",
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ):
        variants = [
            DraftEmailVariant(variant="A", subject=f"A-{company.company_name}", body="body-a", cta=parent.cta_policy),
            DraftEmailVariant(variant="B", subject=f"B-{company.company_name}", body="body-b", cta=parent.cta_policy),
        ]
        if variant_mode == "abc":
            variants.append(
                DraftEmailVariant(variant="C", subject=f"C-{company.company_name}", body="body-c", cta=parent.cta_policy)
            )
        return variants, "A", []


class CampaignTests(unittest.TestCase):
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
            openai_chat_model="gpt-5",
            openai_embedding_model="text-embedding-3-small",
            google_service_account_json=None,
            retention_days=90,
        )

    def _write_csv(self, path: Path) -> None:
        headers = ["Email", "First Name", "Last Name", "companyName", "website", "jobTitle"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
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
            writer.writerow(
                {
                    "Email": "luca@example.com",
                    "First Name": "Luca",
                    "Last Name": "Rossi",
                    "companyName": "Gamma SRL",
                    "website": "https://gamma.it",
                    "jobTitle": "CEO",
                }
            )
            writer.writerow(
                {
                    "Email": "no-website@example.com",
                    "First Name": "No",
                    "Last Name": "Site",
                    "companyName": "Delta SRL",
                    "website": "",
                    "jobTitle": "Owner",
                }
            )

    def test_row_mode_preserves_input_and_marks_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            leads_path = Path(tmpdir) / "leads.csv"
            self._write_csv(leads_path)
            out_dir = Path(tmpdir) / "out"
            store = _FakeStore(self._profile())
            llm = _FakeLLM()

            summary, export_path, rows = run_campaign(
                config=self._config(),
                store=store,
                llm=llm,
                parent_slug="azienda-a",
                leads_csv_path=str(leads_path),
                out_dir=str(out_dir),
                sheet_id=None,
                recipient_mode="row",
                variant_mode="ab",
                output_schema="ab",
                llm_policy="strict",
                enrichment_mode="auto",
                max_concurrency=2,
                max_retries=1,
                backoff_base_seconds=0.0,
            )

            self.assertEqual(summary.rows_total, 3)
            self.assertEqual(summary.rows_valid, 3)
            self.assertEqual(summary.rows_skipped, 0)
            self.assertEqual(len(rows), 3)

            with export_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                exported = list(reader)

            self.assertEqual(len(exported), 3)
            self.assertIn("Email", exported[0])
            self.assertIn("final_subject", exported[0])
            template_only_rows = [
                item for item in exported if "template_only_no_website" in (item.get("risk_flags") or "")
            ]
            self.assertEqual(len(template_only_rows), 1)
            self.assertTrue(template_only_rows[0].get("final_subject"))
            self.assertTrue(template_only_rows[0].get("final_body"))

    def test_output_schema_abc_contains_variant_c_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            leads_path = Path(tmpdir) / "leads.csv"
            self._write_csv(leads_path)
            out_dir = Path(tmpdir) / "out"
            store = _FakeStore(self._profile())
            llm = _FakeLLM()

            _, export_path, _ = run_campaign(
                config=self._config(),
                store=store,
                llm=llm,
                parent_slug="azienda-a",
                leads_csv_path=str(leads_path),
                out_dir=str(out_dir),
                sheet_id=None,
                recipient_mode="row",
                variant_mode="abc",
                output_schema="abc",
                llm_policy="strict",
                enrichment_mode="auto",
                max_concurrency=1,
                max_retries=1,
                backoff_base_seconds=0.0,
            )

            with export_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                headers = reader.fieldnames or []
            self.assertIn("variant_c_subject", headers)
            self.assertIn("variant_c_body", headers)

    def test_cost_cap_blocks_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            leads_path = Path(tmpdir) / "leads.csv"
            self._write_csv(leads_path)
            out_dir = Path(tmpdir) / "out"
            store = _FakeStore(self._profile())
            llm = _FakeLLM()

            with self.assertRaises(ValueError):
                run_campaign(
                    config=self._config(),
                    store=store,
                    llm=llm,
                    parent_slug="azienda-a",
                    leads_csv_path=str(leads_path),
                    out_dir=str(out_dir),
                    sheet_id=None,
                    recipient_mode="row",
                    variant_mode="ab",
                    output_schema="ab",
                    llm_policy="strict",
                    enrichment_mode="auto",
                    max_concurrency=1,
                    max_retries=1,
                    backoff_base_seconds=0.0,
                    cost_cap_eur=0.01,
                    force_cost_override=False,
                )

            summary, _, _ = run_campaign(
                config=self._config(),
                store=store,
                llm=llm,
                parent_slug="azienda-a",
                leads_csv_path=str(leads_path),
                out_dir=str(out_dir),
                sheet_id=None,
                recipient_mode="row",
                variant_mode="ab",
                output_schema="ab",
                llm_policy="strict",
                enrichment_mode="auto",
                max_concurrency=1,
                max_retries=1,
                backoff_base_seconds=0.0,
                cost_cap_eur=0.01,
                force_cost_override=True,
            )
            self.assertGreater(summary.estimated_cost_eur, 0.01)

    def test_selects_passing_variant_when_other_fails_copy_guard(self) -> None:
        class _LLMOneFail(_FakeLLM):
            def generate_campaign_variants(  # type: ignore[override]
                self,
                *,
                parent,
                company,
                contact,
                dossier,
                marketing_snippets,
                variant_mode: str = "ab",
                llm_policy: str = "strict",
                max_retries: int = 3,
                backoff_base_seconds: float = 1.0,
            ):
                variants = [
                    DraftEmailVariant(
                        variant="A",
                        subject=f"A-{company.company_name}",
                        body="body-a",
                        cta=parent.cta_policy,
                        risk_flags=[],
                    ),
                    DraftEmailVariant(
                        variant="B",
                        subject=f"B-{company.company_name}",
                        body="body-b",
                        cta=parent.cta_policy,
                        risk_flags=["failed_copy_guard"],
                    ),
                ]
                return variants, "B", []

        with tempfile.TemporaryDirectory() as tmpdir:
            leads_path = Path(tmpdir) / "leads.csv"
            headers = ["Email", "First Name", "Last Name", "companyName", "website", "jobTitle"]
            with leads_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
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

            out_dir = Path(tmpdir) / "out"
            store = _FakeStore(self._profile())
            llm = _LLMOneFail()

            summary, export_path, _ = run_campaign(
                config=self._config(),
                store=store,
                llm=llm,
                parent_slug="azienda-a",
                leads_csv_path=str(leads_path),
                out_dir=str(out_dir),
                sheet_id=None,
                recipient_mode="row",
                variant_mode="ab",
                output_schema="ab",
                llm_policy="strict",
                enrichment_mode="auto",
                max_concurrency=1,
                max_retries=1,
                backoff_base_seconds=0.0,
            )

            self.assertEqual(summary.rows_failed, 0)
            with export_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                exported = list(reader)
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0].get("generation_status"), "OK")
            self.assertEqual(exported[0].get("selected_variant"), "A")
            self.assertIn("Copy guard fallito", exported[0].get("generation_warning") or "")

    def test_export_auto_schema_uses_summary_metadata(self) -> None:
        class _ExportStore:
            def get_campaign_summary(self, campaign_id: str):
                return {"summary_json": {"output_schema": "abc"}}

            def list_campaign_records(self, campaign_id: str):
                return [
                    {
                        "parent_slug": "azienda-a",
                        "company_name": "Beta SRL",
                        "contact_name": "Anna Verdi",
                        "contact_title": "Founder",
                        "contact_email": "anna@example.com",
                        "status": "PENDING",
                        "reviewer_notes": "",
                        "approved_variant": "",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "payload_json": {
                            "recommended_variant": "A",
                            "selected_variant": "A",
                            "generation_status": "OK",
                            "generation_warning": "",
                            "error_code": "",
                            "dossier": {"evidence": ["Fonte"]},
                            "risk_flags": [],
                            "variants": [
                                {"variant": "A", "subject": "sa", "body": "ba"},
                                {"variant": "B", "subject": "sb", "body": "bb"},
                                {"variant": "C", "subject": "sc", "body": "bc"},
                            ],
                        },
                    }
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "export.csv"
            export_campaign(_ExportStore(), "campaign-test", str(out), output_schema="auto")
            with out.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                headers = reader.fieldnames or []
            self.assertIn("variant_c_subject", headers)


if __name__ == "__main__":
    unittest.main()
