#!/usr/bin/env python3
"""
SonarAI — CLI Entry Point
Usage:
    python main.py --report sonar-report.json \\
                   --repo   https://github.com/owner/repo.git \\
                   --sha    abc123def456
"""

import argparse
import sys

from loguru import logger

# Configure loguru: structured one-line format
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    colorize=True,
    level="INFO",
)
logger.add(
    "log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{line} | {message}",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SonarAI — automated Sonar issue remediation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--report",
        required=True,
        metavar="PATH",
        help="Path to sonar-report.json",
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="URL",
        help="GitHub HTTPS clone URL (e.g. https://github.com/owner/repo.git)",
    )
    parser.add_argument(
        "--sha",
        required=True,
        metavar="SHA",
        help="Exact commit SHA used during the Sonar scan",
    )
    args = parser.parse_args()

    from graph import run_pipeline

    final_state = run_pipeline(
        sonar_report_path=args.report,
        repo_url=args.repo,
        commit_sha=args.sha,
    )

    if final_state.get("pr_url"):
        print(f"\n✅  PR opened: {final_state['pr_url']}")
        return 0
    elif final_state.get("escalation_path"):
        print(f"\n⚠️   Escalation written: {final_state['escalation_path']}")
        return 1
    else:
        print("\nℹ️  Pipeline completed with no PR or escalation.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
