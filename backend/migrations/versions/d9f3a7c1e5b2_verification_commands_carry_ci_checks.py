"""verification commands carry CI checks

Revision ID: d9f3a7c1e5b2
Revises: b6d9e2c4f8a1
Create Date: 2026-08-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9f3a7c1e5b2"
down_revision: str | Sequence[str] | None = "b6d9e2c4f8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for profile_name in ("baseline", "effective"):
        for command_group in ("test_commands", "lint_commands", "typecheck_commands"):
            path = f"{{{profile_name},verification,{command_group}}}"
            op.execute(
                sa.text(
                    f"""
                    UPDATE project_repos
                    SET profile = jsonb_set(
                        profile,
                        '{path}',
                        (
                            SELECT jsonb_agg(
                                CASE jsonb_typeof(entry)
                                    WHEN 'string' THEN jsonb_build_object(
                                        'command', entry #>> '{{}}',
                                        'ci_check', NULL
                                    )
                                    ELSE entry
                                END
                                ORDER BY position
                            )
                            FROM jsonb_array_elements(profile #> '{path}')
                                WITH ORDINALITY AS commands(entry, position)
                        )
                    )
                    WHERE EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(profile #> '{path}') AS commands(entry)
                        WHERE jsonb_typeof(entry) = 'string'
                    )
                    """
                )
            )


def downgrade() -> None:
    for profile_name in ("baseline", "effective"):
        for command_group in ("test_commands", "lint_commands", "typecheck_commands"):
            path = f"{{{profile_name},verification,{command_group}}}"
            op.execute(
                sa.text(
                    f"""
                    UPDATE project_repos
                    SET profile = jsonb_set(
                        profile,
                        '{path}',
                        (
                            SELECT jsonb_agg(
                                CASE jsonb_typeof(entry)
                                    WHEN 'object' THEN entry -> 'command'
                                    ELSE entry
                                END
                                ORDER BY position
                            )
                            FROM jsonb_array_elements(profile #> '{path}')
                                WITH ORDINALITY AS commands(entry, position)
                        )
                    )
                    WHERE EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(profile #> '{path}') AS commands(entry)
                        WHERE jsonb_typeof(entry) = 'object'
                    )
                    """
                )
            )
