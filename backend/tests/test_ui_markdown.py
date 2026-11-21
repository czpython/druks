from datetime import UTC, datetime, timedelta, timezone

import pytest
from druks import ui
from druks.ui.exceptions import NoTextFormError
from druks_field_notes.app import FieldNotes

AT = datetime(2026, 8, 29, 9, 14, 2, tzinfo=UTC)
ENDPOINT = "https://druks.example"


def text(block, *, endpoint=ENDPOINT) -> str:
    return block.to_markdown(app="field_notes", pages=FieldNotes.pages(), endpoint=endpoint)


@pytest.mark.parametrize(
    "block", [ui.Text("Fan *noise*."), ui.Markdown("## Report\n\n**2** stale.")]
)
def test_text_and_markdown_read_as_written(block):
    assert text(block) == block.text


def test_a_quote_reads_as_a_block_quote_and_a_callout_bolds_its_title():
    assert text(ui.Quote("Tuesday works.\n\nThanks.")) == "> Tuesday works.\n>\n> Thanks."
    assert text(ui.Callout("Paste the client.", title="Connect first")) == (
        "> **Connect first**\n>\n> Paste the client."
    )


def test_a_divider_reads_as_a_rule():
    assert text(ui.Divider()) == "---"


def test_layout_blocks_read_as_their_blocks_in_order_under_headings():
    block = ui.Section(
        title="Plan",
        blocks=[
            ui.Card(title="Note 7", description="Fan noise.", blocks=[ui.Text("Rack 3.")]),
            ui.Stack([ui.Columns([ui.Text("left"), ui.Text("right")])]),
            ui.Cards(title="Recent", cards=[ui.Card(title="Note 8"), ui.Card(title="Note 9")]),
        ],
    )

    assert text(block) == (
        "## Plan\n\n"
        "### Note 7\n\nFan noise.\n\nRack 3.\n\n"
        "left\n\nright\n\n"
        "### Recent\n\n### Note 8\n\n### Note 9"
    )


def test_facts_and_metrics_read_as_lists_of_bold_labels_and_their_values():
    facts = ui.Facts([ui.Fact("Owner", "Ada"), ui.Fact("Open", 12)], title="Latest")
    metrics = ui.Metrics([ui.Metric("Latency", ui.NumberValue(40, unit="ms"))])

    assert text(facts) == "### Latest\n\n- **Owner**: Ada\n- **Open**: 12"
    assert text(metrics) == "- **Latency**: 40 ms"


def test_a_list_reads_as_a_list_whose_items_keep_their_lines():
    assert text(ui.List(["Fan noise.\n\nOn rack 3.", "Rack 3."], title="Bodies")) == (
        "### Bodies\n\n- Fan noise.\n  \n  On rack 3.\n- Rack 3."
    )


def test_a_table_reads_as_a_markdown_table_without_its_row_detail():
    table = ui.Table(
        title="Notes",
        columns=[ui.TableColumn("Note"), ui.TableColumn("Answers", align="end")],
        rows=[ui.TableRow(["fan | rack\r\n3", 1234.5], detail="Read twice.", key="7")],
    )

    assert text(table) == (
        "### Notes\n\n| Note | Answers |\n| --- | ---: |\n| fan \\| rack 3 | 1,234.5 |"
    )


def test_a_table_without_rows_reads_as_its_empty_state():
    table = ui.Table(title="Notes", empty=ui.EmptyState("No notes yet", description="Write one."))

    assert text(table) == "### Notes\n\nNo notes yet\n\nWrite one."


@pytest.mark.parametrize(
    "block",
    [
        ui.Facts(title="Latest"),
        ui.List(title="Bodies"),
        ui.Table(title="Notes"),
        ui.Cards(title="Recent"),
        ui.Files(title="Report"),
    ],
)
def test_a_block_with_nothing_in_it_writes_nothing(block):
    assert text(block) == ""


def test_a_chart_reads_as_a_table_with_a_row_for_each_category_and_a_column_for_each_series():
    chart = ui.Chart(
        title="Answers per day",
        categories=["Mon", "Tue"],
        series=[
            ui.ChartSeries(label="peer-7", points=[3, 5]),
            ui.ChartSeries(label="peer-8", points=[0.5, 0]),
        ],
        category_label="Day",
        value_label="Answers",
    )

    assert text(chart) == (
        "### Answers per day — Answers\n\n"
        "| Day | peer-7 | peer-8 |\n| --- | --- | --- |\n| Mon | 3 | 0.5 |\n| Tue | 5 | 0 |"
    )


@pytest.mark.parametrize(
    "progress, written",
    [
        (
            ui.Progress(
                "Stages", steps=[ui.ProgressStep(label="plan", status=ui.StatusValue("done"))]
            ),
            "Stages\n\n- plan: done",
        ),
        (ui.Progress("Sweeping peers", completed=3, total=8), "Sweeping peers: 3 of 8"),
        (ui.Progress("Waiting"), "Waiting: still running"),
    ],
    ids=["steps", "share", "unknown"],
)
def test_progress_reads_as_its_steps_or_its_share(progress, written):
    assert text(progress) == written


def test_a_timeline_lists_its_items_oldest_first_with_the_time_of_each():
    timeline = ui.Timeline(
        [
            ui.TimelineItem(when=AT + timedelta(minutes=46), title="finished"),
            ui.TimelineItem(when=AT, title="started"),
        ],
        title="Sweep",
    )

    assert text(timeline) == (
        "### Sweep\n\n- 2026-08-29 09:14 UTC: started\n- 2026-08-29 10:00 UTC: finished"
    )


@pytest.mark.parametrize(
    "value, written",
    [
        (ui.TextValue("peer-7", description="What the row is about."), "peer-7"),
        (ui.NumberValue(40, unit="ms"), "40 ms"),
        (ui.NumberValue(0.25), "0.25"),
        (ui.NumberValue(0.00005, unit="BTC"), "0.00005 BTC"),
        (ui.StatusValue("live", tone="success"), "live"),
        (
            ui.TimeValue(datetime(2026, 8, 29, 11, 14, tzinfo=timezone(timedelta(hours=2)))),
            "2026-08-29 09:14 UTC",
        ),
        (ui.TimeValue(None, empty="never"), "never"),
        (
            ui.ControlsValue(
                [ui.Link("Docs", url="https://docs.example"), ui.Link("Notes", page="notes")]
            ),
            "[Docs](https://docs.example) · [Notes](https://druks.example/field_notes)",
        ),
    ],
    ids=[
        "text",
        "number with unit",
        "number",
        "small number",
        "status",
        "time",
        "no time",
        "controls",
    ],
)
def test_a_value_reads_as_its_words(value, written):
    assert text(value) == written


@pytest.mark.parametrize(
    "status, written",
    [
        ({"state": "parked", "gate": "review"}, "needs you"),
        ({"state": "running", "gate": None}, "summarizing"),
        ({"state": "scheduled", "gate": None}, "summarizing"),
        ({"state": "failed", "gate": None}, "failed"),
        ({"state": "cancelled", "gate": None}, "cancelled"),
        ({"state": "orphaned", "gate": None}, "orphaned"),
        ({"state": "finished", "gate": None}, "idle"),
        ({"state": None, "gate": None}, "idle"),
    ],
)
def test_a_subject_status_reads_as_the_word_the_dashboard_shows(status, written):
    value = ui.SubjectStatus.model_validate(
        {
            "value": "subject_status",
            "subject": {"subjectType": "note", "subjectId": "7"},
            "working": "summarizing",
            "status": status,
        }
    )

    assert text(value) == written


def test_a_subject_status_read_without_a_status_writes_nothing():
    assert text(ui.SubjectStatus(ui.Follows(subject_type="note", subject_id="7"))) == ""


@pytest.mark.parametrize(
    "block, written",
    [
        (ui.Link("Notes", page="notes"), "[Notes](https://druks.example/field_notes)"),
        (
            ui.Link("History", page="note_history", arguments={"note_id": "rack 3/a"}),
            "[History](https://druks.example/field_notes/notes/rack%203%2Fa/history)",
        ),
        (
            ui.Link("The note", subject=ui.Follows(subject_type="note", subject_id="7")),
            "[The note](https://druks.example/field_notes/notes/7)",
        ),
        (
            ui.Link("The repo", subject=ui.Follows(subject_type="repository", subject_id="acme/x")),
            "[The repo](https://druks.example/field_notes/repository/acme%2Fx)",
        ),
        (ui.Link("Docs", url="https://docs.example/a"), "[Docs](https://docs.example/a)"),
        (
            ui.Link("Settings", url="/settings/connections"),
            "[Settings](https://druks.example/settings/connections)",
        ),
        (
            ui.Image(url="/api/files/shot", alternative_text="The shopfront."),
            "[The shopfront.](https://druks.example/api/files/shot)",
        ),
        (
            ui.Files([ui.FileSummary(id="f1", name="sweep.csv", content_type="text/csv", size=1)]),
            "- [sweep.csv](https://druks.example/api/files/f1)",
        ),
        (
            ui.ImageGallery([ui.Image(url="https://cdn.example/a.png", alternative_text="A.")]),
            "- [A.](https://cdn.example/a.png)",
        ),
        (ui.Link("History", page="note_history"), "History"),
        (ui.Link("Nowhere", page="nowhere"), "Nowhere"),
        (ui.Image(url="data:image/png;base64,AAAA", alternative_text="A dot."), "A dot."),
        (ui.Link("Mirror", url="//cdn.example/a"), "Mirror"),
    ],
    ids=[
        "page",
        "page with encoded arguments",
        "subject with a decision page",
        "subject without one",
        "outside address",
        "dashboard address",
        "image",
        "file",
        "gallery",
        "missing route argument",
        "undeclared page",
        "data address",
        "address of another host with no scheme",
    ],
)
def test_a_link_opens_the_page_the_app_declares_on_the_dashboard(block, written):
    assert text(block) == written


def test_a_link_on_a_value_or_a_card_takes_the_words_it_sits_on():
    note = ui.Link(page="note", arguments={"note_id": 7})

    assert text(ui.TextValue("Note 7", link=note)) == (
        "[Note 7](https://druks.example/field_notes/notes/7)"
    )
    assert text(ui.Card(title="Gist", link=note)) == (
        "### [Gist](https://druks.example/field_notes/notes/7)"
    )


@pytest.mark.parametrize(
    "block, written",
    [
        (ui.Link("Notes", page="notes"), "Notes"),
        (ui.Link("The note", subject=ui.Follows(subject_type="note", subject_id="7")), "The note"),
        (ui.Link("Settings", url="/settings/connections"), "Settings"),
        (ui.Image(url="/api/files/shot", alternative_text="The shopfront."), "The shopfront."),
        (ui.Link("Docs", url="https://docs.example/a"), "[Docs](https://docs.example/a)"),
    ],
    ids=["page", "subject", "dashboard address", "image", "outside address"],
)
def test_without_an_endpoint_only_an_outside_address_stays_a_link(block, written):
    assert text(block, endpoint="") == written


@pytest.mark.parametrize(
    "block",
    [
        ui.Action(label="Go", operation="write_note"),
        ui.Form(action=ui.Action(label="Save", operation="write_note")),
        ui.GateControls(ui.Follows(subject_type="note", subject_id="7")),
    ],
    ids=["Action", "Form", "GateControls"],
)
def test_a_block_the_operator_acts_through_has_no_text_form(block):
    with pytest.raises(NoTextFormError) as refused:
        block.to_markdown()

    assert refused.value.block == type(block).__name__
    assert str(refused.value).startswith(f"{type(block).__name__} has no text form")
