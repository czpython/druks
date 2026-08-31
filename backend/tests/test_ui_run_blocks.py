from datetime import UTC, datetime

import pytest
from druks.files import File
from druks.ui import (
    Files,
    FileSummary,
    Image,
    Page,
    Progress,
    ProgressStep,
    StatusValue,
    Timeline,
    TimelineItem,
)

AT = datetime(2026, 8, 29, 9, 14, 2, tzinfo=UTC)


def wire(*blocks):
    return Page("Run", blocks=list(blocks)).model_dump(by_alias=True, mode="json")["blocks"]


def test_a_timeline_item_carries_its_stamp_and_status():
    (block,) = wire(
        Timeline(
            [
                TimelineItem(
                    when=AT, title="Run started", status=StatusValue("active", tone="active")
                )
            ],
            title="Sweep",
        )
    )

    assert block == {
        "block": "timeline",
        "title": "Sweep",
        "items": [
            {
                "when": "2026-08-29T09:14:02Z",
                "title": "Run started",
                "description": "",
                "status": {"value": "status", "label": "active", "tone": "active", "kind": "state"},
            }
        ],
    }


def test_progress_carries_its_three_shapes():
    determinate, indeterminate, staged = wire(
        Progress("Sweeping peers", completed=3, total=8),
        Progress("Waiting"),
        Progress("Stages", steps=[ProgressStep(label="plan", status=StatusValue("done"))]),
    )

    assert determinate["completed"] == 3.0
    assert determinate["steps"] == []
    assert indeterminate["completed"] is None
    assert staged["steps"] == [
        {
            "label": "plan",
            "status": {"value": "status", "label": "done", "tone": "neutral", "kind": "state"},
        }
    ]


def test_an_image_carries_its_alternative_text():
    (block,) = wire(Image(url="/api/files/a", alternative_text="Flat at 40 ms.", caption="Latency"))

    assert block == {
        "block": "image",
        "url": "/api/files/a",
        "alternativeText": "Flat at 40 ms.",
        "caption": "Latency",
    }


def test_files_take_the_files_primitive():
    file = File(id="018f2c1e", name="sweep.csv", size=4211, content_type="text/csv")

    (block,) = wire(Files([file], title="Report"))

    assert block["files"] == [
        {
            "id": "018f2c1e",
            "name": "sweep.csv",
            "contentType": "text/csv",
            "size": 4211,
            # The platform's own route, which keeps the identity gate.
            "url": "/api/files/018f2c1e",
        }
    ]


def test_a_file_summary_can_be_given_directly():
    summary = FileSummary(id="a", name="n", content_type="text/plain", size=1)

    (block,) = wire(Files([summary]))

    assert block["files"][0]["id"] == "a"


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_an_image_rejects_alternative_text_that_says_nothing(blank):
    with pytest.raises(ValueError):
        Image(url="/api/files/a", alternative_text=blank)


def test_a_timeline_stamp_must_name_a_moment():
    with pytest.raises(ValueError):
        TimelineItem(when=datetime(2026, 8, 29, 9, 14, 2), title="Run started")


def test_a_file_url_always_goes_through_the_platform_route():
    summary = FileSummary(id="a", name="n", content_type="text/plain", size=1)

    assert summary.url == "/api/files/a"


@pytest.mark.parametrize(
    "bad",
    [
        {"completed": 2, "total": 1},
        {"completed": float("nan")},
        {"total": 0},
        {"completed": 1, "steps": [ProgressStep(label="plan", status=StatusValue("done"))]},
    ],
)
def test_progress_takes_one_shape_inside_its_bounds(bad):
    with pytest.raises(ValueError):
        Progress("Sweeping", **bad)


def test_a_timeline_orders_itself_oldest_first():
    timeline = Timeline(
        [
            TimelineItem(when=datetime(2026, 8, 29, 10, 0, tzinfo=UTC), title="finished"),
            TimelineItem(when=datetime(2026, 8, 29, 9, 0, tzinfo=UTC), title="started"),
        ]
    )

    assert [item.title for item in timeline.items] == ["started", "finished"]


def test_a_timeline_keeps_microseconds_apart():
    timeline = Timeline(
        [
            TimelineItem(when=datetime(2026, 8, 29, 9, 0, 0, 900, tzinfo=UTC), title="later"),
            TimelineItem(when=datetime(2026, 8, 29, 9, 0, 0, 100, tzinfo=UTC), title="earlier"),
        ]
    )

    assert [item.title for item in timeline.items] == ["earlier", "later"]
