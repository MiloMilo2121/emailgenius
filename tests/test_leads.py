from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from emailgenius.leads import (
    build_company_and_contacts,
    group_rows_by_company,
    read_leads_csv,
    select_primary_contact,
)


HEADERS = [
    "First Name",
    "Last Name",
    "Full Name",
    "Title",
    "Headline",
    "Seniority",
    "Email",
    "LinkedIn Link",
    "Lead City",
    "Lead State",
    "Lead Country",
    "Company Name",
    "Industry",
    "Employee Count",
    "Cleaned Company Name",
    "MillionVerifier Status",
    "Company Website Full",
    "Company LinkedIn Link",
    "Company Keywords",
    "Company Technologies",
    "Company Short Description",
    "Company Founded Year",
]


class LeadTests(unittest.TestCase):
    def test_grouping_and_primary_contact_selection(self) -> None:
        rows = [
            {
                "First Name": "Mario",
                "Last Name": "Rossi",
                "Full Name": "Mario Rossi",
                "Title": "CEO",
                "Headline": "CEO",
                "Seniority": "c_suite",
                "Email": "mario@example.com",
                "LinkedIn Link": "https://www.linkedin.com/in/mario",
                "Lead City": "Bergamo",
                "Lead State": "Lombardy",
                "Lead Country": "Italy",
                "Company Name": "Acme SRL",
                "Industry": "machinery",
                "Employee Count": "60",
                "Cleaned Company Name": "Acme",
                "MillionVerifier Status": "good",
                "Company Website Full": "https://www.acme.it",
                "Company LinkedIn Link": "https://www.linkedin.com/company/acme",
                "Company Keywords": "automation, b2b",
                "Company Technologies": "WordPress",
                "Company Short Description": "Azienda manifatturiera",
                "Company Founded Year": "1998",
            },
            {
                "First Name": "Luca",
                "Last Name": "Bianchi",
                "Full Name": "Luca Bianchi",
                "Title": "Sales Assistant",
                "Headline": "Sales",
                "Seniority": "entry",
                "Email": "luca@example.com",
                "LinkedIn Link": "https://www.linkedin.com/in/luca",
                "Lead City": "Bergamo",
                "Lead State": "Lombardy",
                "Lead Country": "Italy",
                "Company Name": "Acme SRL",
                "Industry": "machinery",
                "Employee Count": "60",
                "Cleaned Company Name": "Acme",
                "MillionVerifier Status": "good",
                "Company Website Full": "https://www.acme.it",
                "Company LinkedIn Link": "https://www.linkedin.com/company/acme",
                "Company Keywords": "automation, b2b",
                "Company Technologies": "WordPress",
                "Company Short Description": "Azienda manifatturiera",
                "Company Founded Year": "1998",
            },
        ]

        groups = group_rows_by_company(rows)
        self.assertEqual(len(groups), 1)

        company, contacts = build_company_and_contacts(groups["acme"])
        primary = select_primary_contact(contacts)

        self.assertEqual(company.company_key, "acme")
        self.assertEqual(company.company_name, "Acme SRL")
        self.assertIsNotNone(primary)
        self.assertEqual(primary.full_name, "Mario Rossi")
        self.assertTrue(primary.is_primary_contact)

    def test_csv_reader_with_real_header_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "leads.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=HEADERS)
                writer.writeheader()
                writer.writerow({
                    "First Name": "Anna",
                    "Last Name": "Verdi",
                    "Full Name": "Anna Verdi",
                    "Title": "Founder",
                    "Seniority": "founder",
                    "Email": "anna@example.com",
                    "Company Name": "Beta SRL",
                    "Company Website Full": "https://beta.it",
                })

            rows = read_leads_csv(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Company Name"], "Beta SRL")


if __name__ == "__main__":
    unittest.main()
