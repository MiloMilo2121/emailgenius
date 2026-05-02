from __future__ import annotations

import unittest
from unittest.mock import patch

from emailgenius.gdrive import DriveLeadRow
from emailgenius.gdrive import ParentWorkspace
from emailgenius.gdrive import _canonicalize_lead_row
from emailgenius.gdrive import _resolve_parent_slug_for_knowledge_file
from emailgenius.gdrive import _render_sequence_doc
from emailgenius.gdrive import fetch_leads_sheet
from emailgenius.gdrive import sync_knowledge_base
from emailgenius.gdrive import sync_parent_profiles
from emailgenius.types import SequenceResult, SequenceStep


class _FakeStore:
    def __init__(self) -> None:
        self.profiles = []

    def upsert_parent_profile(self, profile, *, set_active: bool = False) -> None:
        self.profiles.append(profile)


class _FakeKnowledgeStore:
    def __init__(self, slugs: list[str], active_slug: str | None = None) -> None:
        self._slugs = slugs
        self._active_slug = active_slug
        self.sync_updates: list[tuple[str, str, str, str]] = []

    def list_parent_profiles(self):
        return [type("P", (), {"slug": slug})() for slug in self._slugs]

    def get_active_parent_slug(self):
        return self._active_slug

    def is_drive_file_synced(self, *, file_id: str, modified_time: str, kind: str) -> bool:
        return False

    def upsert_drive_file_sync_state(self, *, file_id: str, modified_time: str, kind: str, status: str) -> None:
        self.sync_updates.append((file_id, modified_time, kind, status))


class GDriveTests(unittest.TestCase):
    def test_sync_parent_profiles_downloads_yaml(self) -> None:
        payload = b"""
slug: azienda-a
company_name: Azienda A
tone: formale
offer_catalog: [Servizio]
icp: [PMI]
proof_points: [Case]
objections: [Budget]
cta_policy: call
no_go_claims: [garantito]
compliance_notes: [pubblico]
"""
        store = _FakeStore()

        with patch("emailgenius.gdrive._ensure_subfolder", return_value="profiles-folder"), patch(
            "emailgenius.gdrive._list_files_in_folder",
            return_value=[
                {"id": "1", "name": "parent.yaml"},
                {"id": "2", "name": "readme.txt"},
            ],
        ), patch("emailgenius.gdrive._download_drive_bytes", return_value=payload):
            report = sync_parent_profiles("root", store, drive_client=object())

        self.assertEqual(report.synced, 1)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(store.profiles), 1)
        self.assertEqual(store.profiles[0].slug, "azienda-a")

    def test_sync_parent_profiles_uses_parent_workspace_slug(self) -> None:
        payload = b"""
slug: altro-slug
company_name: Azienda B
tone: formale
offer_catalog: [Servizio]
icp: [PMI]
proof_points: [Case]
objections: [Budget]
cta_policy: call
no_go_claims: [garantito]
compliance_notes: [pubblico]
"""
        store = _FakeStore()
        workspace = ParentWorkspace(slug="azienda-b", folder_id="parent-1", profile_folder_id="profile-folder")
        with patch("emailgenius.gdrive._discover_parent_workspaces", return_value=[workspace]), patch(
            "emailgenius.gdrive._list_files_in_folder",
            return_value=[{"id": "1", "name": "profile.yaml"}],
        ), patch("emailgenius.gdrive._download_drive_bytes", return_value=payload):
            report = sync_parent_profiles("root", store, drive_client=object())

        self.assertEqual(report.synced, 1)
        self.assertEqual(store.profiles[0].slug, "azienda-b")

    def test_canonicalize_lead_row_maps_aliases(self) -> None:
        row = {
            "companyName": "Beta SRL",
            "website": "https://beta.example",
            "First Name": "Anna",
            "Last Name": "Verdi",
            "Email": "anna@example.com",
            "parent_slug": "Contributo Facile",
        }
        canonical = _canonicalize_lead_row(row)
        self.assertEqual(canonical["Company Name"], "Beta SRL")
        self.assertEqual(canonical["Company Website Full"], "https://beta.example")
        self.assertEqual(canonical["Full Name"], "Anna Verdi")
        self.assertEqual(canonical["parent_slug"], "contributo-facile")

    def test_render_sequence_doc_contains_all_steps(self) -> None:
        sequence = SequenceResult(
            attack_angle="trigger-based",
            trigger_facts=["Notizia A"],
            steps=[
                SequenceStep(step_id="E1", subject="s1", body="b1", goal="g1"),
                SequenceStep(step_id="E2", subject="s2", body="b2", goal="g2"),
                SequenceStep(step_id="E3", subject="s3", body="b3", goal="g3"),
                SequenceStep(step_id="BREAKUP", subject="s4", body="b4", goal="g4"),
            ],
        )
        text = _render_sequence_doc(company_name="Acme", contact_name="Mario", sequence=sequence)
        self.assertIn("[E1]", text)
        self.assertIn("[E2]", text)
        self.assertIn("[E3]", text)
        self.assertIn("[BREAKUP]", text)

    def test_resolve_parent_slug_for_knowledge_file_from_filename_prefix(self) -> None:
        resolved = _resolve_parent_slug_for_knowledge_file(
            file_name="azienda-b__brochure.pdf",
            known_parent_slugs={"azienda-a", "azienda-b"},
            active_parent_slug="azienda-a",
        )
        self.assertEqual(resolved, "azienda-b")

    def test_resolve_parent_slug_for_knowledge_file_requires_explicit_mapping_with_multiple_parents(self) -> None:
        resolved = _resolve_parent_slug_for_knowledge_file(
            file_name="brochure-generica.pdf",
            known_parent_slugs={"azienda-a", "azienda-b"},
            active_parent_slug="azienda-a",
        )
        self.assertIsNone(resolved)

    def test_resolve_parent_slug_for_knowledge_file_single_parent_fallback(self) -> None:
        resolved = _resolve_parent_slug_for_knowledge_file(
            file_name="brochure-generica.pdf",
            known_parent_slugs={"azienda-a"},
            active_parent_slug=None,
        )
        self.assertEqual(resolved, "azienda-a")

    def test_sync_knowledge_base_marks_failed_when_parent_mapping_is_ambiguous(self) -> None:
        store = _FakeKnowledgeStore(slugs=["azienda-a", "azienda-b"], active_slug="azienda-a")
        drive_file = {
            "id": "file-1",
            "name": "brochure-generica.pdf",
            "modifiedTime": "2026-03-03T10:00:00Z",
        }
        with patch("emailgenius.gdrive._discover_parent_workspaces", return_value=[]), patch(
            "emailgenius.gdrive._ensure_subfolder", side_effect=["knowledge-folder", "processed-folder"]
        ), patch("emailgenius.gdrive._list_files_in_folder", return_value=[drive_file]), patch(
            "emailgenius.gdrive._download_drive_bytes"
        ) as download_mock, patch("emailgenius.gdrive.ingest_knowledge_file") as ingest_mock, patch(
            "emailgenius.gdrive._move_file_to_folder"
        ) as move_mock:
            report = sync_knowledge_base("root", store, llm=object(), drive_client=object())

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.synced, 0)
        self.assertEqual(store.sync_updates[-1][3], "FAILED_PARENT_MAPPING")
        download_mock.assert_not_called()
        ingest_mock.assert_not_called()
        move_mock.assert_not_called()

    def test_fetch_leads_sheet_injects_parent_slug_from_parent_workspace(self) -> None:
        class _Worksheet:
            title = "Sheet1"

            def get_all_values(self):
                return [
                    ["Email", "First Name", "Last Name", "companyName"],
                    ["anna@example.com", "Anna", "Verdi", "Beta SRL"],
                ]

        class _Spreadsheet:
            def worksheets(self):
                return [_Worksheet()]

        class _SheetsClient:
            def open_by_key(self, key: str):
                return _Spreadsheet()

        workspace = ParentWorkspace(slug="azienda-a", folder_id="parent-1", leads_folder_id="leads-folder")
        with patch("emailgenius.gdrive._discover_parent_workspaces", return_value=[workspace]), patch(
            "emailgenius.gdrive._list_files_in_folder",
            return_value=[
                {
                    "id": "sheet-1",
                    "name": "INPUT LEADS",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "modifiedTime": "2026-04-02T10:00:00Z",
                }
            ],
        ):
            rows = fetch_leads_sheet("root", _SheetsClient(), drive_client=object())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].canonical_row["parent_slug"], "azienda-a")
        self.assertIsInstance(rows[0], DriveLeadRow)


class ExecuteWithBackoffTests(unittest.TestCase):
    def test_fatal_status_is_not_retried(self) -> None:
        from emailgenius.gdrive import _execute_with_backoff

        for fatal in (400, 401, 403, 404):
            with self.subTest(status=fatal):
                calls = {"n": 0}

                class FakeErr(Exception):
                    pass

                def boom():
                    calls["n"] += 1
                    err = FakeErr(f"http {fatal}")
                    err.status_code = fatal  # type: ignore[attr-defined]
                    raise err

                with self.assertRaises(FakeErr):
                    _execute_with_backoff(boom, max_attempts=4)
                self.assertEqual(calls["n"], 1, f"status {fatal} should not retry")

    def test_retryable_status_is_retried_then_succeeds(self) -> None:
        from emailgenius.gdrive import _execute_with_backoff

        attempts = {"n": 0}

        class FakeErr(Exception):
            pass

        def maybe():
            attempts["n"] += 1
            if attempts["n"] < 3:
                err = FakeErr("503")
                err.status_code = 503  # type: ignore[attr-defined]
                raise err
            return "ok"

        with patch("emailgenius.gdrive.time.sleep"):
            result = _execute_with_backoff(maybe, max_attempts=5)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 3)

    def test_unknown_error_is_not_retried(self) -> None:
        from emailgenius.gdrive import _execute_with_backoff

        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("no status, no retry")

        with self.assertRaises(RuntimeError):
            _execute_with_backoff(boom, max_attempts=5)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
