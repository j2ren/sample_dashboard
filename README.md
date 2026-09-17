# PostHog impact analysis

## Pull-request activity

Use `scripts/pr_activity_90d.py` to count pull requests created in the last 90
days and their distinct human authors. It uses Python's standard library and
calls GitHub directly; you do not need to install or authenticate the GitHub
CLI.

```bash
export GITHUB_TOKEN=... # token with read access to PostHog/posthog
python3 scripts/pr_activity_90d.py
```

`GH_TOKEN` is also supported. The token is read only from the process
environment and is never written to disk or printed.

The script pages through the GitHub pull-request API in descending creation
order, stopping once it reaches the requested cutoff. It excludes bot PRs when
GitHub marks the author as `type: Bot`, when the login ends in `[bot]`, or when
you explicitly provide a known bot login:

```bash
python3 scripts/pr_activity_90d.py --bot-login renovate --bot-login dependabot
```

## Issue-link coverage

Check whether the proposed issue-delivery score has enough coverage before
using it as a primary metric:

```bash
python3 scripts/issue_link_coverage_90d.py --sample 10
```

This reports the share of merged, non-bot PRs in the last 90 days that GitHub
itself says close one or more issues, and prints linked and unlinked samples
for review. It never counts bot-authored PRs. Future contribution scoring must
also ignore commits authored by bots, even when the containing PR is human-authored.

For a faster spot-check of a fixed period, use an exclusive end date:

```bash
python3 scripts/issue_link_coverage_90d.py --start 2026-09-10 --end 2026-09-17
```

To validate from the Issue page's direction instead, inspect 500 recently
updated closed issues and their associated merged human PRs:

```bash
python3 scripts/closed_issue_pr_links.py --limit 500 --sample 10
```

To calculate the first deterministic issue-delivery score for the rolling
90-day window, run:

```bash
python3 scripts/score_issue_delivery.py --output issue_delivery_scores.json
```

Each issue is worth one point, split evenly among its unique human commit
authors. The script excludes bot PRs, bot or unknown commit authors, unmerged
PRs, PRs from other repositories, and direct Git-reverted PRs. A direct revert
is detected only when a commit message explicitly says `This reverts commit
<SHA>` and that SHA matches the PR's merge commit. A direct revert-of-a-revert
restores the original PR's eligibility.

Check whether a priority-weighted score is supported by label usage:

```bash
python3 scripts/priority_label_audit.py --days 90 --chunk-days 1
```

Add `--all-labels` to print every label used in the selected issues and PRs.

## Interactive dashboard

After generating `issue_delivery_scores.json`, start a local server from this
directory and open the displayed address in a browser:

```bash
python3 -m http.server 8000
```

The dashboard is available at `http://localhost:8000`. It shows the top five
engineers by default, supports selecting an engineer to inspect their
issue/PR-level evidence, and can expand to the full ranking.
