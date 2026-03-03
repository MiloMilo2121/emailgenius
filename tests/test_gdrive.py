from __future__ import annotations

import unittest
from unittest.mock import patch

from emailgenius.gdrive import _canonicalize_lead_row
from emailgenius.gdrive import _render_sequence_doc
from emailgenius.gdrive import sync_parent_profiles
from emailgenius.types import SequenceResult, SequenceStep


class _FakeStore:
    def __init__(self) -> None:
        self.profiles = []

    def upsert_parent_profile(self, profile, *, set_active: bool = False) -> None:
        self.profiles.append(profile)


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


if __name__ == "__main__":
    unittest.main()
