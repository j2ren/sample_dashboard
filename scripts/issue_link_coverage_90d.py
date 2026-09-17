#!/usr/bin/env python3
"""Measure how often merged, human-authored PRs close GitHub issues.

Requires GITHUB_TOKEN (or GH_TOKEN) with read access to the repository. The
script queries GitHub's ``closingIssuesReferences`` relationship; it does not
guess from PR body text such as "fixes #123".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


QUERY = """
query MergedPullRequests($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
      issueCount
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
        number
        title
        url
        mergedAt
        author { login __typename }
        closingIssuesReferences(first: 100) {
          totalCount
          nodes { number title url }
        }
      }
      }
    }
}
"""


def github_query(token: str, variables: dict) -> dict:
    request = Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "posthog-impact-analysis",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach GitHub API: {error.reason}") from error
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload["data"]


def is_bot(author: dict | None, extra_bot_logins: set[str]) -> bool:
    """Recognize GitHub Bot actors, conventional bot logins, and configured bots."""
    if not author:
        return False  # Preserve deleted/unknown human authors in the denominator.
    login = author["login"].lower()
    return (
        author["__typename"] == "Bot"
        or login.endswith("[bot]")
        or login in extra_bot_logins
    )


def print_sample(label: str, pulls: list[dict], sample_size: int) -> None:
    print(f"\n{label} (up to {sample_size}):")
    for pr in pulls[:sample_size]:
        issues = pr["closingIssuesReferences"]
        issue_text = ", ".join(f"#{issue['number']} {issue['url']}" for issue in issues["nodes"])
        if issues["totalCount"] > len(issues["nodes"]):
            issue_text += f" (+{issues['totalCount'] - len(issues['nodes'])} more)"
        if not issue_text:
            issue_text = "no GitHub closing-issue link"
        author = pr["author"]["login"] if pr["author"] else "<deleted user>"
        print(f"  PR #{pr['number']} by {author}: {pr['url']}")
        print(f"    {pr['title']} | {issue_text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="PostHog/posthog", help="owner/repository")
    parser.add_argument("--days", type=int, default=90, help="rolling lookback period")
    parser.add_argument("--start", help="UTC start date (YYYY-MM-DD); requires --end")
    parser.add_argument("--end", help="UTC end date, exclusive (YYYY-MM-DD); requires --start")
    parser.add_argument("--chunk-days", type=int, default=7, help="GitHub search date-range size")
    parser.add_argument("--sample", type=int, default=10, help="PRs to show in each spot-check sample")
    parser.add_argument("--bot-login", action="append", default=[], help="additional bot login to exclude")
    args = parser.parse_args()
    if args.days < 1 or args.chunk_days < 1 or args.sample < 0:
        parser.error("--days and --chunk-days must be positive and --sample cannot be negative")
    try:
        owner, name = args.repo.split("/", 1)
    except ValueError:
        parser.error("--repo must be in owner/repository format")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        parser.error("set GITHUB_TOKEN (or GH_TOKEN) before running")

    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be used together")
    if args.start:
        try:
            cutoff = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
            now = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        except ValueError:
            parser.error("--start and --end must be YYYY-MM-DD")
        if now <= cutoff:
            parser.error("--end must be after --start")
    else:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=args.days)
    extra_bots = {login.lower() for login in args.bot_login}
    human_pulls: list[dict] = []
    bot_pulls = 0
    linked_bot_pulls = 0
    chunk_start = datetime.combine(cutoff.date(), datetime.min.time(), tzinfo=timezone.utc)
    search_end = (
        now
        if args.start
        else datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    )
    # Search is split into small date ranges. This avoids GitHub Search's 1,000
    # result cap and avoids walking the repository's entire PR history.
    while chunk_start < search_end:
        chunk_end = min(chunk_start + timedelta(days=args.chunk_days), search_end)
        print(
            f"Fetching merged PRs from {chunk_start.date().isoformat()} through "
            f"{(chunk_end - timedelta(days=1)).date().isoformat()}...",
            file=sys.stderr,
            flush=True,
        )
        query = (
            f"repo:{owner}/{name} is:pr is:merged "
            f"merged:>={chunk_start.date().isoformat()} merged:<{chunk_end.date().isoformat()}"
        )
        cursor = None
        while True:
            data = github_query(token, {"query": query, "cursor": cursor})
            connection = data["search"]
            for pr in connection["nodes"]:
                # Search should only return PRs, but retain this guard if GitHub
                # returns another Issue subtype unexpectedly.
                if not pr or not pr.get("mergedAt"):
                    continue
                merged_at = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
                if not (cutoff <= merged_at < now):
                    continue
                if is_bot(pr["author"], extra_bots):
                    bot_pulls += 1
                    if pr["closingIssuesReferences"]["totalCount"] > 0:
                        linked_bot_pulls += 1
                    continue
                human_pulls.append(pr)
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
        chunk_start = chunk_end

    linked = [pr for pr in human_pulls if pr["closingIssuesReferences"]["totalCount"] > 0]
    unlinked = [pr for pr in human_pulls if pr["closingIssuesReferences"]["totalCount"] == 0]
    rate = (100 * len(linked) / len(human_pulls)) if human_pulls else 0
    print(f"Repository: {args.repo}")
    window_days = (now - cutoff).total_seconds() / 86_400
    print(f"Window: {cutoff.date().isoformat()} through {now.date().isoformat()} UTC ({window_days:g} days)")
    print(f"Merged human PRs: {len(human_pulls)}")
    print(f"Merged bot PRs excluded: {bot_pulls}")
    print(f"Excluded bot PRs with a GitHub closing-issue link: {linked_bot_pulls}")
    print(f"Human PRs closing one or more GitHub issues: {len(linked)} ({rate:.1f}%)")
    print(f"Human PRs without a GitHub closing-issue link: {len(unlinked)} ({100 - rate:.1f}%)")
    print("Rule: a PR is linked only when GitHub exposes closingIssuesReferences; body-text guesses are not counted.")
    print("Bot rule: author __typename == 'Bot', login ending '[bot]', or --bot-login.")
    print_sample("Linked PRs to verify", linked, args.sample)
    print_sample("Unlinked PRs to verify", unlinked, args.sample)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
