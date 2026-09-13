from druks.contrib.software_factory.datastructures import PullRequest
from druks.contrib.software_factory.workflows import PullRequestReview
from druks.testing import seed_run

from software_factory.factories import make_test_work_item, seed_build_run

_OVERVIEW = "/api/software_factory/pages"


def _lanes(page: dict) -> dict[str, list[dict]]:
    (lanes,) = page["blocks"]
    return {table["title"]: table["rows"] for table in lanes["blocks"]}


def _entry(row: dict) -> tuple[str, str]:
    work, status, _ = row["cells"]
    return work["text"], status["label"]


async def test_a_running_review_is_in_flight_beside_the_builds(druks_client, druks_db):
    item = await make_test_work_item(repo="acme/app", title="Fix login", ticket_key="ENG-40")
    await seed_build_run(druks_db, work_item_id=item.id, state="running")
    await seed_run(druks_db, kind=PullRequestReview.kind, subject=PullRequest.get("acme/app", 7))

    page = (await druks_client.get(_OVERVIEW)).json()

    lanes = _lanes(page)
    assert sorted(_entry(row) for row in lanes["In flight"]) == [
        ("Fix login", "Build…"),
        ("acme/app#7", "Pull request review…"),
    ]
    assert lanes["Needs you"] == []
    links = [row["cells"][0]["link"]["subject"] for row in lanes["In flight"]]
    assert {"subjectType": "pull_request", "subjectId": "acme/app#7"} in links
    # Either kind of change reads the lanes again.
    assert page["follows"] == {"subjectType": "work_item", "subjectId": ""}
    assert page["blocks"][0]["follows"] == {"subjectType": "pull_request", "subjectId": ""}


async def test_a_stopped_review_needs_you_with_its_failure(druks_client, druks_db):
    item = await make_test_work_item(repo="acme/app", title="Fix login")
    await seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review_work")
    await seed_run(
        druks_db,
        kind=PullRequestReview.kind,
        subject=PullRequest.get("acme/app", 7),
        state="failed",
        failure="the reviewer broke its contract",
    )

    lanes = _lanes((await druks_client.get(_OVERVIEW)).json())

    rows = {row["cells"][0]["text"]: row for row in lanes["Needs you"]}
    assert rows["Fix login"]["cells"][1]["label"] == "Review implementation"
    assert rows["acme/app#7"]["cells"][1]["label"] == "failed"
    assert rows["acme/app#7"]["detail"] == "the reviewer broke its contract"
    assert lanes["In flight"] == []


async def test_the_filter_narrows_both_lanes(druks_client, druks_db):
    item = await make_test_work_item(repo="acme/app", title="Fix login")
    await seed_build_run(druks_db, work_item_id=item.id, state="running")
    await seed_run(druks_db, kind=PullRequestReview.kind, subject=PullRequest.get("beta/web", 3))

    lanes = _lanes((await druks_client.get(_OVERVIEW, params={"query": "BETA"})).json())
    assert [_entry(row) for row in lanes["In flight"]] == [("beta/web#3", "Pull request review…")]

    page = (await druks_client.get(_OVERVIEW, params={"query": "ENG-99"})).json()
    assert [table["emptyText"] for table in page["blocks"][0]["blocks"]] == [
        'No work matches "ENG-99".',
        'No work matches "ENG-99".',
    ]
