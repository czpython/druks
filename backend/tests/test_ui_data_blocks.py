from datetime import UTC, datetime

import pytest
from druks.ui import (
    Action,
    Callout,
    Card,
    Cards,
    Chart,
    ChartSeries,
    Columns,
    ControlsValue,
    EmptyState,
    Fact,
    Facts,
    Link,
    List,
    Metric,
    Metrics,
    NumberValue,
    Page,
    Stack,
    StatusValue,
    Table,
    TableColumn,
    TableRow,
    Text,
    TextField,
    TextValue,
    TimeValue,
)

AT = datetime(2026, 8, 29, 9, 14, 2, tzinfo=UTC)


def wire(*blocks):
    return Page("x", blocks=list(blocks)).model_dump(by_alias=True, mode="json")["blocks"]


def test_a_chart_carries_its_series_axes_and_labels():
    (block,) = wire(
        Chart(
            kind="bar",
            title="Answers per day",
            categories=["Mon", "Tue"],
            series=[ChartSeries(label="peer-7", points=[3, 5])],
            category_label="Day",
            value_label="Answers",
        )
    )

    assert block == {
        "block": "chart",
        "kind": "bar",
        "title": "Answers per day",
        "categories": ["Mon", "Tue"],
        "series": [{"label": "peer-7", "points": [3.0, 5.0]}],
        "categoryLabel": "Day",
        "valueLabel": "Answers",
    }


def test_a_series_must_carry_one_point_for_each_category():
    with pytest.raises(ValueError, match="one point for each"):
        Chart(categories=["Mon"], series=[ChartSeries(label="peer-7", points=[3, 5])])


def test_a_table_row_must_carry_one_cell_for_each_column():
    with pytest.raises(ValueError, match="cells under"):
        Table(columns=[TableColumn("Peer")], rows=[TableRow([])])


def test_a_table_cell_can_reach_another_page():
    (block,) = wire(
        Table(
            columns=[TableColumn("Peer"), TableColumn("Answers", align="end")],
            rows=[
                TableRow(
                    [
                        TextValue(
                            "peer-7", link=Link("peer-7", page="peer", arguments={"peer_id": "7"})
                        ),
                        NumberValue(12),
                    ]
                )
            ],
            empty_text="No peers yet.",
        )
    )

    assert block["columns"] == [
        {"label": "Peer", "align": "start"},
        {"label": "Answers", "align": "end"},
    ]
    assert block["rows"][0]["cells"][0]["link"]["page"] == "peer"
    assert block["emptyText"] == "No peers yet."


def test_a_table_can_select_rows_for_its_actions():
    (block,) = wire(
        Table(
            columns=[TableColumn("Peer")],
            rows=[TableRow([TextValue("peer-7")], key="7")],
            select="peer_ids",
            actions=[Action(label="Park", operation="write_note", tone="danger")],
        )
    )

    assert block["select"] == "peer_ids"
    assert block["rows"][0]["key"] == "7"
    assert [action["label"] for action in block["actions"]] == ["Park"]


def test_a_selectable_table_needs_actions_and_a_key_on_every_row():
    with pytest.raises(ValueError, match="has actions and no select"):
        Table(
            columns=[TableColumn("Peer")],
            rows=[TableRow([TextValue("peer-7")], key="7")],
            actions=[Action(label="Park", operation="write_note")],
        )
    with pytest.raises(ValueError, match="names select and has no actions"):
        Table(
            columns=[TableColumn("Peer")],
            rows=[TableRow([TextValue("peer-7")], key="7")],
            select="peer_ids",
        )
    with pytest.raises(ValueError, match="a row has no key"):
        Table(
            columns=[TableColumn("Peer")],
            rows=[TableRow([TextValue("peer-7")])],
            select="peer_ids",
            actions=[Action(label="Park", operation="write_note")],
        )
    with pytest.raises(ValueError, match="two rows keyed"):
        Table(
            columns=[TableColumn("Peer")],
            rows=[
                TableRow([TextValue("a")], key="7"),
                TableRow([TextValue("b")], key="7"),
            ],
            select="peer_ids",
            actions=[Action(label="Park", operation="write_note")],
        )


def test_a_table_action_cannot_collect_fields():
    with pytest.raises(ValueError, match="collects fields"):
        Table(
            columns=[TableColumn("Peer")],
            rows=[TableRow([TextValue("peer-7")], key="7")],
            select="peer_ids",
            actions=[
                Action(
                    label="Park",
                    operation="write_note",
                    fields=[TextField(name="why", label="Why")],
                )
            ],
        )


def test_a_table_cell_can_hold_actions():
    (block,) = wire(
        Table(
            columns=[TableColumn("Do")],
            rows=[
                TableRow(
                    [
                        ControlsValue(
                            [
                                Action(label="Go", operation="write_note", tone="primary"),
                                Link("Home", url="/"),
                            ]
                        )
                    ]
                )
            ],
        )
    )

    cell = block["rows"][0]["cells"][0]
    assert cell["value"] == "controls"
    assert [control["label"] for control in cell["controls"]] == ["Go", "Home"]


def test_a_status_can_reach_the_thing_it_names():
    (facts,) = wire(
        Facts(
            [
                Fact(
                    "Site",
                    value=StatusValue(
                        "live", tone="success", link=Link("live", url="https://ada.example")
                    ),
                )
            ]
        )
    )

    assert facts["facts"][0]["value"] == {
        "value": "status",
        "label": "live",
        "tone": "success",
        "link": {
            "block": "link",
            "label": "live",
            "page": "",
            "arguments": {},
            "url": "https://ada.example",
            "subject": None,
        },
    }


def test_every_value_carries_its_own_discriminator():
    (facts,) = wire(
        Facts(
            [
                Fact("Name", value=TextValue("peer-7")),
                Fact("Answers", value=NumberValue(40, unit="ms")),
                Fact("State", value=StatusValue("parked", tone="warning")),
                Fact("When", value=TimeValue(AT)),
                Fact("Do", value=ControlsValue([Action(label="Go", operation="write_note")])),
            ]
        )
    )

    assert [fact["value"]["value"] for fact in facts["facts"]] == [
        "text",
        "number",
        "status",
        "time",
        "controls",
    ]
    assert facts["facts"][1]["value"] == {
        "value": "number",
        "number": 40.0,
        "unit": "ms",
        "tone": "neutral",
    }
    assert facts["facts"][3]["value"] == {"value": "time", "when": "2026-08-29T09:14:02Z"}


def test_metrics_hold_metrics_and_a_list_holds_values():
    metrics, items = wire(
        Metrics([Metric("Open", value=NumberValue(12), description="d")]),
        List([TextValue("Fan noise.")], title="Recent"),
    )

    assert metrics["metrics"][0]["label"] == "Open"
    assert metrics["metrics"][0]["description"] == "d"
    assert items["items"][0]["text"] == "Fan noise."


def test_layout_blocks_hold_every_block_including_each_other():
    (stack,) = wire(
        Stack(
            [
                Columns([Text("left"), Stack([Text("nested")])]),
                Facts([Fact("a", value=TextValue("b"))]),
            ],
            gap="large",
        )
    )

    assert stack["gap"] == "large"
    columns = stack["blocks"][0]
    assert columns["block"] == "columns"
    assert columns["blocks"][1]["blocks"][0]["text"] == "nested"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_number_must_be_one_json_can_carry(bad):
    with pytest.raises(ValueError):
        NumberValue(bad)
    with pytest.raises(ValueError):
        ChartSeries(label="s", points=[bad])


def test_cards_finds_an_action_in_a_card_and_in_its_empty_state():
    """Boot checks every action against the app's operations, so an action the
    walk misses is one that ships naming a route nobody declares."""
    block = Cards(
        cards=[Card(title="Peer 7", controls=[Action(label="Retire", operation="retire_peer")])],
        empty=EmptyState("No peer yet", controls=[Action(label="Scan", operation="scan")]),
        drop=Action(label="Move", operation="move_peer"),
    )

    assert [action.operation for action in block.iter_actions()] == [
        "move_peer",
        "retire_peer",
        "scan",
    ]


def test_a_callout_carries_its_next_step():
    block = Callout(
        "Paste the client.",
        title="Connect first",
        controls=[
            Link("Settings → Connections → Services", url="/settings/connections"),
            Action(label="Retry", operation="retry_connect"),
        ],
    )

    assert wire(block)[0]["controls"][0]["url"] == "/settings/connections"
    assert [action.operation for action in block.iter_actions()] == ["retry_connect"]


def test_cards_drop_cannot_collect_fields_or_confirm():
    with pytest.raises(ValueError, match="drop is the submit"):
        Cards(
            drop=Action(
                label="Move",
                operation="move_peer",
                fields=[TextField(name="reason", label="Reason")],
            )
        )
    with pytest.raises(ValueError, match="drop is the submit"):
        Cards(drop=Action(label="Move", operation="move_peer", confirm="Move this peer?"))


def test_cards_carries_stack_layout_drop_and_card_drag():
    (block,) = wire(
        Cards(
            layout="stack",
            drop=Action(
                label="Move",
                operation="move_peer",
                arguments={"status": "todo"},
            ),
            cards=[Card(title="peer-7", drag={"identifier": "P-7"})],
        )
    )

    assert block["layout"] == "stack"
    assert block["drop"]["operation"] == "move_peer"
    assert block["drop"]["arguments"] == {"status": "todo"}
    assert block["cards"][0]["drag"] == {"identifier": "P-7"}


def test_cards_with_none_and_nothing_to_say_carries_no_empty_state():
    assert Cards().empty is None
    assert Cards().layout == "wrap"
    assert Cards().drop is None
    assert Card().drag == {}
