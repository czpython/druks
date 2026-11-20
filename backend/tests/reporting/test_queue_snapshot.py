"""queue-snapshot.py against a fake work item board."""

import json
from datetime import UTC, datetime

BUILD = "software_factory.build"
BOARD = "/api/software_factory/work_item"


def row(work_item_id, state, run, kind=BUILD):
    return {"summary": {"id": work_item_id}, "status": {"state": state, "run": run, "kind": kind}}


def snapshots_of(checkout):
    return sorted((checkout / ".tmp" / "queue").iterdir())


def test_snapshot_counts_each_build_that_waits_for_a_box_once(
    dashboard, cookie_file, run_script, checkout, capsys
):
    dashboard.serve_json(
        BOARD,
        {
            "rows": [
                row("7", "running", "run-a"),
                row("8", "running", "run-b"),
                row("9", "finished", "run-c"),
                row("10", "running", "run-d", kind="software_factory.pull_request_review"),
                row("11", "scheduled", "run-e"),
                row("12", "running", "run-a"),
                row("13", None, None, kind=None),
            ]
        },
    )
    dashboard.serve_json(f"{BOARD}/7", {"phase": "queued_for_vm"})
    dashboard.serve_json(f"{BOARD}/8", {"phase": "agent_running"})
    dashboard.serve_json(f"{BOARD}/11", {"phase": None})
    dashboard.serve_json(f"{BOARD}/12", {"phase": "queued_for_vm"})

    run_script("queue-snapshot.py", "--base", dashboard.base, "--cookie", cookie_file)

    [snapshot] = snapshots_of(checkout)
    assert [
        path.relative_to(checkout).as_posix()
        for path in checkout.rglob("*")
        if path.is_file() and "scripts" not in path.relative_to(checkout).parts
    ] == [f".tmp/queue/{snapshot.name}"]
    assert json.loads(snapshot.read_text()) == {
        "taken_at": datetime.strptime(snapshot.name, "%Y%m%dT%H%M%S.%fZ.json")
        .replace(tzinfo=UTC)
        .isoformat(),
        "board_rows": 7,
        "in_flight_builds": ["run-a", "run-b", "run-e"],
        "queued_for_vm": ["run-a"],
    }
    assert dashboard.paths == [BOARD, f"{BOARD}/7", f"{BOARD}/8", f"{BOARD}/11", f"{BOARD}/12"]
    assert {
        (request.get_method(), request.get_header("Cookie")) for request in dashboard.requests
    } == {("GET", dashboard.cookie)}
    output = capsys.readouterr().out
    assert output.splitlines() == ["3 builds in flight, 1 waiting for a box", str(snapshot)]
    assert dashboard.cookie not in output + snapshot.read_text()


def test_each_invocation_writes_its_own_snapshot(dashboard, cookie_file, run_script, checkout):
    dashboard.serve_json(
        BOARD, {"rows": [row("7", "running", "run-a"), row("8", "running", "run-b")]}
    )
    dashboard.serve_json(f"{BOARD}/7", {"phase": "queued_for_vm"})
    dashboard.serve_json(f"{BOARD}/8", {"phase": "agent_running"})
    run_script("queue-snapshot.py", "--base", dashboard.base, "--cookie", cookie_file)
    dashboard.serve_json(f"{BOARD}/7", {"phase": "agent_running"})
    dashboard.serve_json(f"{BOARD}/8", {"phase": "queued_for_vm"})
    run_script("queue-snapshot.py", "--base", dashboard.base, "--cookie", cookie_file)

    assert [json.loads(path.read_text())["queued_for_vm"] for path in snapshots_of(checkout)] == [
        ["run-a"],
        ["run-b"],
    ]
