#!/usr/bin/env python3
"""Audit merged human PRs associated with recently closed GitHub issues.

Requires GITHUB_TOKEN (or GH_TOKEN). GitHub Search cannot sort by ``closedAt``;
"recent" therefore means recently *updated* closed issues, which is the closest
available API ordering. Bot PRs are always excluded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


QUERY = """
query ClosedIssues($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        number
        title
        url
        closedAt
        closedByPullRequestsReferences(first: 100, includeClosedPrs: true) {
          nodes {
            number
            title
            url
            mergedAt
            repository { nameWithOwner }
            author { login __typename }
          }
        }
      }
    }
  }
}
"""


def query_github(token: str, variables: dict) -> dict:
    request = Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "posthog-impact-analysis",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {error.read().decode(errors='replace')}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach GitHub API: {error.reason}") from error
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload["data"]["search"]


def is_bot(author: dict | None, extra_bot_logins: set[str]) -> bool:
    if not author:
        return True  # Unknown accounts cannot be credited to an engineer.
    login = author["login"].lower()
    return author["__typename"] == "Bot" or login.endswith("[bot]") or login in extra_bot_logins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="PostHog/posthog", help="owner/repository")
    parser.add_argument("--limit", type=int, default=500, help="closed issues to inspect (1-1,000)")
    parser.add_argument("--sample", type=int, default=10, help="human issue→PR associations to print")
    parser.add_argument("--bot-login", action="append", default=[], help="additional bot login to exclude")
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000 or args.sample < 0:
        parser.error("--limit must be 1-1,000 and --sample cannot be negative")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        parser.error("set GITHUB_TOKEN (or GH_TOKEN) before running")

    bots = {login.lower() for login in args.bot_login}
    issue_query = f"repo:{args.repo} is:issue is:closed sort:updated-desc"
    issues: list[dict] = []
    cursor = None
    while len(issues) < args.limit:
        print(f"Fetching closed issues {len(issues) + 1}-{min(len(issues) + 100, args.limit)}...", file=sys.stderr, flush=True)
        result = query_github(token, {"query": issue_query, "cursor": cursor})
        page = result["nodes"]
        if not page:
            break
        issues.extend(page)
        if not result["pageInfo"]["hasNextPage"]:
            break
        cursor = result["pageInfo"]["endCursor"]
    issues = issues[: args.limit]

    human_associations: list[tuple[dict, dict]] = []
    bot_or_unknown_links = 0
    unmerged_human_links = 0
    cross_repository_links = 0
    for issue in issues:
        for pr in issue["closedByPullRequestsReferences"]["nodes"]:
            if pr["repository"]["nameWithOwner"].lower() != args.repo.lower():
                cross_repository_links += 1
                continue
            if is_bot(pr["author"], bots):
                bot_or_unknown_links += 1
            elif not pr["mergedAt"]:
                unmerged_human_links += 1
            else:
                human_associations.append((issue, pr))

    issue_numbers_with_human_pr = {issue["number"] for issue, _ in human_associations}
    unique_human_prs = {pr["url"] for _, pr in human_associations}
    print(f"Repository: {args.repo}")
    print(f"Closed issues inspected: {len(issues)} (sorted by most recently updated)")
    print(f"Issues with one or more associated merged human PRs: {len(issue_numbers_with_human_pr)}")
    print(f"Merged human issue→PR associations: {len(human_associations)}")
    print(f"Distinct merged human PRs: {len(unique_human_prs)}")
    print(f"Bot or unknown-author PR links excluded: {bot_or_unknown_links}")
    print(f"Unmerged human PR links excluded: {unmerged_human_links}")
    print(f"Cross-repository PR links excluded: {cross_repository_links}")
    print("Bot rule: GitHub Bot actor, login ending '[bot]', explicit --bot-login, or unknown author.")
    print(f"\nSample merged human associations (up to {args.sample}):")
    for issue, pr in human_associations[: args.sample]:
        print(f"  Issue #{issue['number']}: {issue['url']}")
        print(f"    PR #{pr['number']} by {pr['author']['login']}: {pr['url']}")
        print(f"    {pr['title']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
