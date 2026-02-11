from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import analyze_company_sync, discover_and_analyze_company_sync, result_to_dict


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

    discover_parser = subparsers.add_parser(
        "discover",
        help="Discover website + news from company name/city, then analyze",
    )
    discover_parser.add_argument("--company", required=True, help="Company display name")
    discover_parser.add_argument("--city", help="City used for discovery query")
    discover_parser.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    discover_parser.add_argument("--show-email", action="store_true", help="Print outreach draft")
    discover_parser.add_argument("--show-news", action="store_true", help="Print top discovered news links")
    discover_parser.add_argument("--news-results", type=int, default=6, help="Number of news links to include")
    discover_parser.add_argument("--site-results", type=int, default=8, help="Number of website candidates")
    discover_parser.add_argument("--out", help="Output JSON path")
    return parser


def _persist_result(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        _persist_result(payload, output_path)

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

    if args.command == "discover":
        try:
            result = discover_and_analyze_company_sync(
                company_name=args.company,
                city=args.city,
                headless=not args.headful,
                site_max_results=args.site_results,
                news_max_results=args.news_results,
            )
        except RuntimeError as exc:
            print(f"Discovery failed: {exc}")
            return 1

        payload = result_to_dict(result)
        city_part = f"-{_slugify(args.city)}" if args.city else ""
        output_path = Path(args.out) if args.out else Path("reports") / f"{_slugify(args.company)}{city_part}.json"
        _persist_result(payload, output_path)

        print(f"Saved analysis to: {output_path}")
        if result.discovery and result.discovery.selected_site:
            print(f"Selected website: {result.discovery.selected_site.url}")
            print(f"Site query: {result.discovery.site_query}")
            print(f"News query: {result.discovery.news_query}")
        print(
            "Eligibility: "
            f"{result.eligibility.eligible} | rate={result.eligibility.estimated_credit_rate} "
            f"| confidence={result.eligibility.confidence}"
        )

        if args.show_news and result.discovery:
            print("\n--- News Links ---")
            for index, item in enumerate(result.discovery.news_results, start=1):
                print(f"{index}. {item.title} -> {item.url}")

        if args.show_email:
            print("\n--- Outreach Draft ---\n")
            print(result.outreach_email)

        return 0

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
