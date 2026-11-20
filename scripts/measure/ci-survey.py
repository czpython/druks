"""For every work item since a date: per implement call, minutes spent waiting on CI in-turn,
minutes running the full suite, pushes, and whether it hit the R28 single-commit rule.
GET only. It writes ci-survey.json beside itself.

    python3 -I ci-survey.py --since 2026-11-01 --base https://druks.example
"""

import argparse
import http.client
import json
import os
import sys
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fetch = __import__("fetch-event-transcripts")
long_runs = __import__("find-long-runs")
classify = __import__("classify")


def scan(lines):
    def ts(v):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))

    uses, ci, suite, pushes, r28, first, last = {}, 0.0, 0.0, 0, False, None, None
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = entry.get("timestamp")
        if stamp:
            first = first or ts(stamp)
            last = ts(stamp)
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):  # ast-grep-ignore: rule-3
            continue
        for block in content:
            if not isinstance(block, dict):  # ast-grep-ignore: rule-3
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash" and stamp:
                command = (block.get("input") or {}).get("command", "")
                if "git push" in command:
                    pushes += 1
                group = classify.classify(command)
                if group != "work":
                    uses[block["id"]] = (ts(stamp), group)
            if block.get("type") == "tool_result":
                text = json.dumps(block.get("content"))
                if "R28" in text:
                    r28 = True
                if stamp and block.get("tool_use_id") in uses:
                    started, kind = uses.pop(block["tool_use_id"])
                    minutes = (ts(stamp) - started).total_seconds() / 60
                    if kind == "waiting":
                        ci += minutes
                    else:
                        suite += minutes
    total = (last - first).total_seconds() / 60 if first and last else 0
    return total, ci, suite, pushes, r28


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--cookie", default="~/.druks-cloud-cookie")
    args = parser.parse_args()
    api = fetch.Api(args.base, fetch.read_cookie(args.cookie))
    since = args.since + "T00:00:00Z"
    rows = []
    for (app, subject_type, subject_id), _ in long_runs.subjects_since(api, "software_factory", since).items():
        if subject_type != "work_item":
            continue
        subject = api.json(fetch.subject_path(app, subject_type, subject_id))
        label = (subject.get("summary") or {}).get("ticketKey") or str(subject_id)
        calls = [c for run in subject.get("timeline", []) for c in run.get("agentCalls", [])
                 if c["agent"].endswith(".implement")]
        if not calls:
            continue
        agg = [0.0, 0.0, 0.0, 0, False, len(calls)]
        for call in calls:
            path = f"/api/{app}/transcripts/{call['id']}/files/stdout.jsonl?disposition=attachment"
            try:
                with api._open(path) as response:
                    lines = response.read().decode("utf-8", "replace").splitlines()
            except (OSError, http.client.HTTPException):
                continue
            total, ci, suite, pushes, r28 = scan(lines)
            agg[0] += total
            agg[1] += ci
            agg[2] += suite
            agg[3] += pushes
            agg[4] = agg[4] or r28
        rows.append((label, *agg))
        print(f"  scanned {label}", file=sys.stderr)
    rows.sort(key=lambda r: -r[2])
    print(f"{'ticket':<12}{'impl calls':>11}{'impl min':>9}{'CI wait':>8}{'suite':>7}{'pushes':>7}  R28")
    for label, total, ci, suite, pushes, r28, n in rows:
        print(f"{label:<12}{n:>11}{total:>9.0f}{ci:>8.0f}{suite:>7.0f}{pushes:>7}  {'yes' if r28 else ''}")
    print(json.dumps({"tickets": len(rows), "rows": rows}), file=open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci-survey.json"), "w"))


if __name__ == "__main__":
    main()
