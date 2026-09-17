#!/usr/bin/env python3
"""Find priority labels and measure their 90-day use on issues and PRs."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

QUERY = """
query LabelAudit($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue { labels(first: 100) { nodes { name } } }
      ... on PullRequest { labels(first: 100) { nodes { name } } }
    }
  }
}
"""


def github(token: str, query: str, variables: dict | None = None) -> dict:
    data = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = Request("https://api.github.com/graphql", data=data, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "posthog-impact-analysis"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {error.read().decode(errors='replace')}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach GitHub API: {error.reason}") from error
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload["data"]


def audit_kind(token: str, repo: str, kind: str, date_field: str, start: datetime, end: datetime, chunk_days: int) -> tuple[int, Counter]:
    """Count labels on closed issues or merged PRs, split into date-safe chunks."""
    labels = Counter()
    total = 0
    chunk_start = datetime.combine(start.date(), datetime.min.time(), tzinfo=timezone.utc)
    search_end = datetime.combine(end.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    while chunk_start < search_end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), search_end)
        final_day = (chunk_end - timedelta(days=1)).date()
        date_filter = f"{date_field}:{chunk_start.date()}" if chunk_start.date() == final_day else f"{date_field}:{chunk_start.date()}..{final_day}"
        search = f"repo:{repo} is:{kind} is:{'merged' if kind == 'pr' else 'closed'} {date_filter}"
        cursor = None
        while True:
            result = github(token, QUERY, {"query": search, "cursor": cursor})["search"]
            if result["issueCount"] > 1000:
                raise RuntimeError(f"GitHub search returned {result['issueCount']} {kind}s for {date_filter}; reduce the chunk size")
            for item in result["nodes"]:
                total += 1
                labels.update(label["name"] for label in item["labels"]["nodes"])
            if not result["pageInfo"]["hasNextPage"]:
                break
            cursor = result["pageInfo"]["endCursor"]
        chunk_start = chunk_end
    return total, labels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="PostHog/posthog")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--chunk-days", type=int, default=1)
    parser.add_argument("--all-labels", action="store_true", help="print every label used in the window")
    args = parser.parse_args()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token or args.days < 1 or args.chunk_days < 1:
        parser.error("set GITHUB_TOKEN (or GH_TOKEN) and provide a positive --days")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    issue_total, issue_labels = audit_kind(token, args.repo, "issue", "closed", cutoff, now, args.chunk_days)
    pr_total, pr_labels = audit_kind(token, args.repo, "pr", "merged", cutoff, now, args.chunk_days)
    priority = re.compile(r"(?:^|[\s:_-])p[0-3](?:$|[\s:_-])|priority", re.IGNORECASE)
    priority_labels = sorted({label for label in issue_labels | pr_labels if priority.search(label)})
    print(f"Repository: {args.repo}")
    print(f"Window: {cutoff.date()} through {now.date()} UTC ({args.days} days)")
    print(f"Closed issues inspected: {issue_total}")
    print(f"Merged PRs inspected: {pr_total}")
    print("Priority-like labels found:")
    for label in priority_labels or ["<none>"]:
        print(f"  {label}: issues={issue_labels[label]}, PRs={pr_labels[label]}")
    if args.all_labels:
        print("\nAll labels used in the window:")
        labels = sorted(issue_labels | pr_labels, key=lambda label: (-(issue_labels[label] + pr_labels[label]), label.lower()))
        for label in labels or ["<none>"]:
            print(f"  {label}: issues={issue_labels[label]}, PRs={pr_labels[label]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
