#!/usr/bin/env python3
"""Count the Software Factory builds that wait for a box, through the dashboard API (GET only).

    python3 queue-snapshot.py --base https://druks.example
    python3 queue-snapshot.py --base https://druks.example --cookie ~/.druks-cloud-cookie

The script reads the work item board, then the detail of each build in flight for its phase.
A build waits for a box while its phase is queued_for_vm. The script counts each build run
once. Each invocation writes one new snapshot to .tmp/queue/ in this repository.
Authentication is the same cookie file as fetch-event-transcripts.py.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fetch = __import__("fetch-event-transcripts")

BOARD = "/api/software_factory/work_item"
BUILD = "software_factory.build"
# The work item board shows a row in these states as in flight.
IN_FLIGHT_STATES = ("running", "scheduled")
SNAPSHOTS = Path(__file__).resolve().parents[2] / ".tmp" / "queue"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--cookie", default="~/.druks-cloud-cookie", help="header file")
    args = parser.parse_args()

    api = fetch.Api(args.base, fetch.read_cookie(args.cookie))
    taken_at = datetime.now(timezone.utc)
    rows = api.json(BOARD)["rows"]
    in_flight_runs, queued_runs = set(), set()
    for row in rows:
        status = row["status"]
        if status["kind"] == BUILD and status["state"] in IN_FLIGHT_STATES:
            in_flight_runs.add(status["run"])
            if api.json(f"{BOARD}/{row['summary']['id']}")["phase"] == "queued_for_vm":
                queued_runs.add(status["run"])

    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    snapshot = SNAPSHOTS / f"{taken_at:%Y%m%dT%H%M%S.%fZ}.json"
    # Mode "x" never replaces an earlier snapshot.
    with snapshot.open("x") as handle:
        json.dump(
            {
                "taken_at": taken_at.isoformat(),
                "board_rows": len(rows),
                "in_flight_builds": sorted(in_flight_runs),
                "queued_for_vm": sorted(queued_runs),
            },
            handle,
            indent=1,
        )
    print(
        f"{len(in_flight_runs)} builds in flight, {len(queued_runs)} waiting for a box\n{snapshot}"
    )


if __name__ == "__main__":
    main()
