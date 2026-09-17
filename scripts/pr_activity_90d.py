#!/usr/bin/env python3
"""Count human-authored pull requests created in a rolling time window.

Requires a GitHub token in ``GITHUB_TOKEN`` (or ``GH_TOKEN``):

    export GITHUB_TOKEN=...  # token needs read access to the repository
    python3 scripts/pr_activity_90d.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def github_pulls(repo: str, page: int, token: str) -> list[dict]:
    """Fetch one page directly from GitHub without storing the token."""
    query = urlencode(
        {"state": "all", "sort": "created", "direction": "desc", "per_page": 100, "page": page}
    )
    request = Request(
        f"https://api.github.com/repos/{repo}/pulls?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "posthog-pr-activity-script",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach the GitHub API: {error.reason}") from error


def is_bot(pr: dict, extra_bot_logins: set[str]) -> bool:
    """Return whether a PR author is a GitHub bot or an explicitly named bot."""
    author = pr.get("user") or {}
    login = (author.get("login") or "").lower()
    return (
        author.get("type") == "Bot"
        or login.endswith("[bot]")
        or login in extra_bot_logins
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="PostHog/posthog", help="owner/repository")
    parser.add_argument("--days", type=int, default=90, help="rolling lookback period")
    parser.add_argument(
        "--bot-login",
        action="append",
        default=[],
        help="additional bot login to exclude (repeatable)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        parser.error("set GITHUB_TOKEN (or GH_TOKEN) to a GitHub access token before running")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    extra_bot_logins = {login.lower() for login in args.bot_login}
    human_prs: list[dict] = []
    excluded_bots: dict[str, int] = {}

    for page in range(1, 10_001):
        pulls = github_pulls(args.repo, page, token)
        if not pulls:
            break

        for pr in pulls:
            created_at = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
            # Results are newest-first, so nothing on subsequent pages can qualify.
            if created_at < cutoff:
                break
            author = pr.get("user") or {}
            login = author.get("login") or "<deleted user>"
            if is_bot(pr, extra_bot_logins):
                excluded_bots[login] = excluded_bots.get(login, 0) + 1
            else:
                human_prs.append(pr)
        else:
            continue
        break

    authors = {pr["user"]["login"] for pr in human_prs if pr.get("user")}
    print(f"Repository: {args.repo}")
    print(f"Window: {cutoff.date().isoformat()} through {now.date().isoformat()} UTC ({args.days} days)")
    print(f"Human-authored PRs created: {len(human_prs)}")
    print(f"Distinct human PR authors: {len(authors)}")
    print(f"Bot PRs excluded: {sum(excluded_bots.values())}")
    if excluded_bots:
        print("Excluded bot logins:")
        for login, count in sorted(excluded_bots.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {login}: {count}")
    print("Bot rule: GitHub user.type == 'Bot', login ending in '[bot]', or --bot-login.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
