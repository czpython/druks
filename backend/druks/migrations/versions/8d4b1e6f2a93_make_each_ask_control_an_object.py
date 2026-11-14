"""Each control of an in-app ask is an object that says whether its answer needs a
note, so every place that takes an answer reads one rule."""

from alembic import op

revision = "8d4b1e6f2a93"
down_revision = "11c6087812ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A run that holds an ask from before this revision gets the rule that Druks used
    # then: a request for changes needs a note, unless the ask carries context.
    op.execute(
        "UPDATE durable_runs SET input_request = jsonb_set(input_request, '{controls}', ("
        "SELECT coalesce(jsonb_agg(jsonb_build_object('id', choice, 'note_required', "
        "choice = 'request_changes' "
        "AND btrim(coalesce(input_request ->> 'context', '')) = '') ORDER BY position), "
        "CAST('[]' AS jsonb)) "
        "FROM jsonb_array_elements_text(input_request -> 'controls') "
        "WITH ORDINALITY AS listed(choice, position))) "
        "WHERE input_request ->> 'presentation' = 'in_app' "
        "AND jsonb_typeof(input_request -> 'controls' -> 0) = 'string'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE durable_runs SET input_request = jsonb_set(input_request, '{controls}', ("
        "SELECT coalesce(jsonb_agg(control -> 'id' ORDER BY position), CAST('[]' AS jsonb)) "
        "FROM jsonb_array_elements(input_request -> 'controls') "
        "WITH ORDINALITY AS listed(control, position))) "
        "WHERE input_request ->> 'presentation' = 'in_app' "
        "AND jsonb_typeof(input_request -> 'controls' -> 0) = 'object'"
    )
