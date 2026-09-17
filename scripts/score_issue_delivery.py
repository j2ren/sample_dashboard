#!/usr/bin/env python3
"""Score engineers from issue-side PR associations in a rolling window.

Each issue is worth one total point, split evenly among its unique human commit
authors. A PR linked to two issues can earn a contributor a share from each;
multiple PRs linked to one issue do not create extra points.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

QUERY = """
query ClosedIssues($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on Issue {
      number url closedAt
      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) { totalCount nodes {
        number url mergedAt repository { nameWithOwner } author { login __typename }
        mergeCommit { oid }
        commits(first: 100) { totalCount nodes { commit { author { user { login __typename } } } } }
      } }
    } }
  }
}
"""


def api(token: str, variables: dict) -> dict:
    request = Request("https://api.github.com/graphql", data=json.dumps({"query": QUERY, "variables": variables}).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "posthog-impact-analysis"})
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


def direct_reverted_commits(token: str, repo: str, cutoff: datetime) -> dict[str, str]:
    """Find active direct reverts on the default branch since the cutoff.

    GitHub commit search indexes only the default branch. The query is ordered
    oldest-first so a revert-of-a-revert restores the original commit correctly.
    """
    revert_pairs: list[tuple[str, str]] = []
    page = 1
    pattern = re.compile(r"This reverts commit ([0-9a-f]{7,40})\.", re.IGNORECASE)
    while True:
        query = urlencode({
            "q": f'repo:{repo} "This reverts commit" committer-date:>={cutoff.date().isoformat()}',
            "per_page": 100,
            "page": page,
            "sort": "committer-date",
            "order": "asc",
        })
        request = Request(
            f"https://api.github.com/search/commits?{query}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "posthog-impact-analysis"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub commit search returned HTTP {error.code}: {error.read().decode(errors='replace')}") from error
        except URLError as error:
            raise RuntimeError(f"Could not reach GitHub API: {error.reason}") from error
        for commit in payload["items"]:
            revert_pairs.extend(
                (commit["sha"].lower(), match.group(1).lower())
                for match in pattern.finditer(commit["commit"]["message"])
            )
        if len(payload["items"]) < 100:
            break
        page += 1

    # Reverting a revert toggles the original commit back to eligible.
    source_to_target = {source: target for source, target in revert_pairs}

    def root_commit(target: str) -> str:
        source = next((sha for sha in source_to_target if sha.startswith(target)), None)
        return root_commit(source_to_target[source]) if source else target

    active: dict[str, str] = {}
    for reverter, target in revert_pairs:
        root = root_commit(target)
        if root in active:
            del active[root]
        else:
            active[root] = reverter
    return active


def is_bot(actor: dict | None, named_bots: set[str]) -> bool:
    if not actor:
        return True  # Unknown identities cannot be attributed to an engineer.
    login = actor["login"].lower()
    return actor["__typename"] == "Bot" or login.endswith("[bot]") or login in named_bots


def score_issue(issue: dict, repo: str, named_bots: set[str], reverted_commits: dict[str, str]) -> tuple[dict[str, set[str]], Counter, list[dict]]:
    """Return human contributors and PR evidence for one closed GitHub issue.

    The dictionary keys are GitHub logins. Each contributor is returned once,
    even if they committed to multiple PRs associated with this same issue.
    """
    links = issue["closedByPullRequestsReferences"]
    if links["totalCount"] > 20:
        raise RuntimeError(
            f"Issue #{issue['number']} has more than 20 associated PRs; "
            "paginate that issue before scoring it rather than silently truncating"
        )

    contributor_prs: dict[str, set[str]] = {}
    skipped = Counter()
    reverted_prs: list[dict] = []
    for pr in links["nodes"]:
        if pr["repository"]["nameWithOwner"].lower() != repo.lower():
            skipped["cross_repository_pr"] += 1
            continue
        if not pr["mergedAt"]:
            skipped["unmerged_pr"] += 1
            continue
        merge_commit = pr["mergeCommit"] and pr["mergeCommit"]["oid"].lower()
        revert_commit = next((sha for original, sha in reverted_commits.items() if merge_commit and merge_commit.startswith(original)), None)
        if revert_commit:
            skipped["directly_reverted_pr"] += 1
            reverted_prs.append({"issue": issue["number"], "pr": pr["url"], "revert_commit": f"https://github.com/{repo}/commit/{revert_commit}"})
            continue
        if is_bot(pr["author"], named_bots):
            skipped["bot_pr"] += 1
            continue
        if pr["commits"]["totalCount"] > 100:
            skipped["pr_with_over_100_commits"] += 1
        for node in pr["commits"]["nodes"]:
            commit_author = node["commit"]["author"]
            author = commit_author and commit_author["user"]
            if is_bot(author, named_bots):
                skipped["bot_or_unknown_commit"] += 1
                continue
            contributor_prs.setdefault(author["login"], set()).add(pr["url"])
    return contributor_prs, skipped, reverted_prs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="PostHog/posthog")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--chunk-days", type=int, default=1, help="closed-issue date range per GitHub search")
    parser.add_argument("--bot-login", action="append", default=[])
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args()
    if args.days < 1 or args.chunk_days < 1:
        parser.error("--days and --chunk-days must be positive")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        parser.error("set GITHUB_TOKEN (or GH_TOKEN) before running")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    named_bots = {login.lower() for login in args.bot_login}
    print("Finding direct Git reverts...", file=sys.stderr, flush=True)
    reverted_commits = direct_reverted_commits(token, args.repo, cutoff)
    start = datetime.combine(cutoff.date(), datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    issue_engineers: dict[int, set[str]] = {}
    evidence: dict[tuple[int, str], set[str]] = {}
    reverted_evidence: dict[str, dict] = {}
    skipped = Counter()

    while start < end:
        finish = min(start + timedelta(days=args.chunk_days), end)
        print(f"Fetching issues closed {start.date()} through {(finish - timedelta(days=1)).date()}...", file=sys.stderr, flush=True)
        cursor = None
        final_date = (finish - timedelta(days=1)).date()
        closed_filter = f"closed:{start.date()}" if start.date() == final_date else f"closed:{start.date()}..{final_date}"
        query = f"repo:{args.repo} is:issue is:closed {closed_filter} sort:updated-desc"
        while True:
            result = api(token, {"query": query, "cursor": cursor})
            # GitHub Search exposes at most 1,000 results for a query. A daily
            # range that hits this limit must be investigated rather than silently truncated.
            if result["issueCount"] > 1000:
                raise RuntimeError(f"{start.date()} has {result['issueCount']} closed issues; GitHub Search caps a query at 1,000 results")
            for issue in result["nodes"]:
                closed_at = datetime.fromisoformat(issue["closedAt"].replace("Z", "+00:00"))
                if not cutoff <= closed_at < now:
                    continue
                contributors = issue_engineers.setdefault(issue["number"], set())
                contributor_prs, issue_skipped, reverted_prs = score_issue(issue, args.repo, named_bots, reverted_commits)
                skipped.update(issue_skipped)
                for reverted_pr in reverted_prs:
                    record = reverted_evidence.setdefault(reverted_pr["pr"], {**reverted_pr, "issues": set()})
                    record["issues"].add(issue["number"])
                for login, pr_urls in contributor_prs.items():
                    contributors.add(login)
                    evidence.setdefault((issue["number"], login), set()).update(pr_urls)
            if not result["pageInfo"]["hasNextPage"]:
                break
            cursor = result["pageInfo"]["endCursor"]
        start = finish

    scores: Counter[str] = Counter()
    for engineers in issue_engineers.values():
        if engineers:
            share = 1 / len(engineers)
            for engineer in engineers:
                scores[engineer] += share
    payload = {
        "repo": args.repo, "cutoff": cutoff.isoformat(), "generated_at": now.isoformat(),
        "rule": "Each issue is worth one point, split evenly across unique human commit authors; bot PRs, direct Git-reverted PRs, and bot/unknown commit authors are excluded.",
        "scores": [{"engineer": login, "points": round(points, 4)} for login, points in scores.most_common()],
        "evidence": [{"issue": issue, "engineer": login, "prs": sorted(prs)} for (issue, login), prs in sorted(evidence.items())],
        "skipped": dict(skipped),
        "direct_revert_commit_references_found": len(reverted_commits),
        "reverted_prs": [{**record, "issues": sorted(record["issues"])} for record in reverted_evidence.values()],
    }
    print(f"Human engineers scored: {len(scores)}")
    print("Top 10:")
    for login, points in scores.most_common(10): print(f"  {login}: {points:.2f}")
    print(f"Skipped: {dict(skipped)}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file: json.dump(payload, file, indent=2)
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr); raise SystemExit(1)
