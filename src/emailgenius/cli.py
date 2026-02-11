from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import analyze_company_sync, result_to_dict


def _slugify(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    lowered = value.lower().replace(" ", "-")
    cleaned = "".join(ch for ch in lowered if ch in allowed)
    return cleaned or "analysis"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emailgenius")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a company website")
    analyze_parser.add_argument("url", help="Company website URL")
    analyze_parser.add_argument("--company", default="Azienda", help="Company display name")
    analyze_parser.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    analyze_parser.add_argument("--show-email", action="store_true", help="Print outreach draft")
    analyze_parser.add_argument("--out", help="Output JSON path")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "analyze":
        result = analyze_company_sync(
            url=args.url,
            company_name=args.company,
            headless=not args.headful,
        )
        payload = result_to_dict(result)

        output_path = Path(args.out) if args.out else Path("reports") / f"{_slugify(args.company)}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"Saved analysis to: {output_path}")
        print(
            "Eligibility: "
            f"{result.eligibility.eligible} | rate={result.eligibility.estimated_credit_rate} "
            f"| confidence={result.eligibility.confidence}"
        )

        if args.show_email:
            print("\n--- Outreach Draft ---\n")
            print(result.outreach_email)

        return 0

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
