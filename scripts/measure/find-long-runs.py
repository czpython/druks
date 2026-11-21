#!/usr/bin/env python3
"""Rank Software Factory work items by agent time, through the dashboard API (GET only).

    python3 find-long-runs.py --since 2026-11-01 --top 15 --base https://druks.example
    python3 find-long-runs.py --since 2026-11-01 --top 15 --fetch 5 --base https://druks.example

Prints one row per work item: agent minutes, calls, input tokens, cost, wall span, and an
event id to pass to fetch-event-transcripts.py. --fetch N runs that script for the top N.
Authentication is the same cookie file as fetch-event-transcripts.py.
"""

import argparse
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fetch = __import__("fetch-event-transcripts")


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def subjects_since(api, app, since):
    """Each subject that has an event since ``since``: its newest event id."""
    seen, before = {}, None
    while True:
        query = {"app": app, "from": since, "limit": 500}
        if before:
            query["before"] = before
        page = api.json("/api/events?" + urllib.parse.urlencode(query))
        for item in page.get("items", []):
            key = (item.get("app"), item.get("subjectType"), item.get("subjectId"))
            if all(key) and key not in seen:
                seen[key] = item["seq"]
        before = page.get("nextCursor") or page.get("next_cursor")
        if not before:
            return seen


def measure(subject):
    calls, minutes, tokens, cost, starts, ends = 0, 0.0, 0, 0.0, [], []
    for run in subject.get("timeline", []):
        for call in run.get("agentCalls", []):
            started, finished = parse(call.get("startedAt")), parse(call.get("finishedAt"))
            calls += 1
            if started and finished:
                minutes += (finished - started).total_seconds() / 60
                starts.append(started)
                ends.append(finished)
            tokens += (call.get("tokens") or {}).get("inputTokens") or 0
            cost += call.get("costUsd") or 0
    span = (max(ends) - min(starts)).total_seconds() / 3600 if starts else 0
    return calls, minutes, tokens, cost, span


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="ISO date, e.g. 2026-11-01")
    parser.add_argument("--app", default="software_factory")
    parser.add_argument("--type", default="work_item", help="subject type to rank")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--fetch", type=int, default=0, help="download the top N")
    parser.add_argument("--tickets", default="", help="also download these, e.g. DRU-1200,DRU-1045")
    parser.add_argument("--base", required=True)
    parser.add_argument("--cookie", default="~/.druks-cloud-cookie")
    parser.add_argument("--out", default="~/druks-transcripts")
    args = parser.parse_args()

    api = fetch.Api(args.base, fetch.read_cookie(args.cookie))
    since = args.since if "T" in args.since else args.since + "T00:00:00Z"
    subjects = subjects_since(api, args.app, since)
    rows = []
    for (app, subject_type, subject_id), seq in subjects.items():
        if subject_type != args.type:
            continue
        subject = api.json(fetch.subject_path(app, subject_type, subject_id))
        label = (subject.get("summary") or {}).get("ticketKey") or f"{subject_type}-{subject_id}"
        rows.append((*measure(subject), label, seq))
    rows.sort(key=lambda row: row[1], reverse=True)

    print(f"{'ticket':<12} {'agent min':>9} {'calls':>5} {'in tokens':>10} {'cost':>8} "
          f"{'span h':>7} {'event':>7}")
    for calls, minutes, tokens, cost, span, label, seq in rows[: args.top]:
        print(f"{label:<12} {minutes:>9.0f} {calls:>5} {tokens / 1e6:>9.1f}M "
              f"${cost:>7.2f} {span:>7.1f} {seq:>7}")
    print("\nAgent minutes exclude PR reviews, which run on the pull_request subject.")

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch-event-transcripts.py")
    wanted = {ticket.strip() for ticket in args.tickets.split(",") if ticket.strip()}
    chosen = rows[: args.fetch] + [row for row in rows[args.fetch :] if row[5] in wanted]
    missing = wanted - {row[5] for row in rows}
    if missing:
        print(f"\nnot found since {args.since}: {', '.join(sorted(missing))}")
    for *_, label, seq in chosen:
        print(f"\nfetching {label} (event {seq})")
        subprocess.run([sys.executable, script, str(seq), "--base", args.base,
                        "--cookie", args.cookie, "--out", args.out], check=False)


if __name__ == "__main__":
    main()
