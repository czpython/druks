import json
from pathlib import Path
from typing import get_args

import pytest
from druks import ui
from druks.ui.blocks import Interactive
from druks.ui.exceptions import NoTextFormError

# The page the dashboard tests render: one of every block the contract carries.
CATALOG = json.loads((Path(__file__).parents[2] / "frontend/src/druksui/catalog.json").read_text())
BLOCKS = {model.model_fields["block"].default: model for model in get_args(get_args(ui.Block)[0])}
VALUES = {model.model_fields["value"].default: model for model in get_args(get_args(ui.Value)[0])}
REFUSED = {kind for kind, model in BLOCKS.items() if issubclass(model, Interactive)}


def entries(value):
    """Every block and value in ``value``, however deep."""
    if isinstance(value, list):
        for item in value:
            yield from entries(item)
    if isinstance(value, dict):
        if value.get("block") in BLOCKS or (
            isinstance(value.get("value"), str) and value["value"] in VALUES
        ):
            yield value
        for item in value.values():
            yield from entries(item)


def kind(entry) -> str:
    return entry.get("block") or entry["value"]


def without_refused(value):
    """``value`` with every block the operator acts through taken out, and the
    selection that their actions ran on."""
    if isinstance(value, list):
        return [without_refused(item) for item in value if not is_refused(item)]
    if is_refused(value):
        return None
    if isinstance(value, dict):
        kept = {key: without_refused(item) for key, item in value.items()}
        if "select" in kept and not kept["actions"]:
            kept["select"] = ""
        return kept
    return value


def is_refused(value) -> bool:
    return isinstance(value, dict) and value.get("block") in REFUSED


def read(entry):
    # A catalog value carries the status that its page was served with, and keeps it.
    return (BLOCKS.get(entry.get("block")) or VALUES[entry["value"]]).model_validate(entry)


ENTRIES = list(entries(CATALOG["blocks"]))


def test_only_the_blocks_the_operator_acts_through_have_no_text_form():
    assert {BLOCKS[refused] for refused in REFUSED} == {ui.Action, ui.Form, ui.GateControls}


def test_the_catalog_carries_every_block_and_value():
    # So that a block added to the contract is in the catalog, and its text form is checked.
    assert {kind(entry) for entry in ENTRIES} == set(BLOCKS) | set(VALUES)


@pytest.mark.parametrize(
    "entry",
    [entry for entry in ENTRIES if not is_refused(entry) and without_refused(entry) == entry],
    ids=kind,
)
def test_every_catalog_entry_with_nothing_refused_writes_markdown(entry):
    assert isinstance(read(entry).to_markdown(), str)


@pytest.mark.parametrize(
    "entry",
    [entry for entry in ENTRIES if not is_refused(entry) and without_refused(entry) != entry],
    ids=kind,
)
def test_a_catalog_block_that_holds_a_refused_one_writes_markdown_without_it(entry):
    with pytest.raises(NoTextFormError):
        read(entry).to_markdown()

    assert isinstance(read(without_refused(entry)).to_markdown(), str)
