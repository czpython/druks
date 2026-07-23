"""baseline schema

Collapsed the pre-1.0 migration chain into one create-all baseline
(schema proven identical to the prior head f0a1b2c3d4e5).

Revision ID: 40cfd4c2aeee
Revises:
Create Date: 2026-07-19 01:12:59.769302

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "40cfd4c2aeee"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # citext backs the case-insensitive account/provider email columns below.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.create_table(
        "accounts",
        sa.Column("username", postgresql.CITEXT(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=True),
        sa.Column("subject_type", sa.String(), nullable=True),
        sa.Column("extension", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "events_subject_idx", "events", ["subject_type", "subject_id", "created_at"], unique=False
    )
    op.create_table(
        "harnesses",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("fast_mode", sa.Boolean(), nullable=False),
        sa.Column("effort", sa.String(), nullable=False),
        sa.Column("timeout", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("models_fetched", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("models_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "mcp_oauth_grants",
        sa.Column("server_name", sa.String(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("token_endpoint", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret", sa.LargeBinary(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_name"),
    )
    op.create_table(
        "mcp_servers",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.Column("token_source", sa.String(), server_default="static", nullable=False),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "secret_headers", sa.LargeBinary(), server_default=sa.text("''::bytea"), nullable=False
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "notification_destinations",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "settings_overrides",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "skill_collections",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source"),
    )
    op.create_table(
        "durable_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("input_gate", sa.String(), nullable=True),
        sa.Column("input_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure", sa.String(), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "harness_logins",
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("provider_email", postgresql.CITEXT(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("harness", "account_id"),
    )
    op.create_table(
        "project_repos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=True),
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_name"),
    )
    op.create_index("project_repos_project_idx", "project_repos", ["project_id"], unique=False)
    op.create_table(
        "skills",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("collection_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["skill_collections.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "usage_scrapes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parse_ok", sa.Boolean(), nullable=False),
        sa.Column("raw_output", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("plan_tier", sa.String(), nullable=True),
        sa.Column("five_hour_percent_left", sa.Integer(), nullable=True),
        sa.Column("five_hour_resets_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("week_percent_left", sa.Integer(), nullable=True),
        sa.Column("week_resets_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlimited", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "usage_scrapes_account_harness_time_idx",
        "usage_scrapes",
        ["account_id", "harness", "scraped_at"],
        unique=False,
    )
    op.create_table(
        "user_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("gate_park_destination_id", sa.String(), nullable=True),
        sa.Column("fallback_account_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fallback_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["gate_park_destination_id"], ["notification_destinations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_calls",
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=True),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sandbox_host_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["durable_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "agent_calls_account_finished_idx",
        "agent_calls",
        ["account_id", "finished_at"],
        unique=False,
    )
    op.create_index("agent_calls_run_idx", "agent_calls", ["run_id"], unique=False)
    op.create_table(
        "notifications",
        sa.Column("subject", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("actions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("run_parked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deep_link", sa.String(), nullable=True),
        sa.Column("destination_id", sa.String(), nullable=False),
        sa.Column("correlation_token", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["notification_destinations.id"],
        ),
        sa.ForeignKeyConstraint(["run_id"], ["durable_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("correlation_token"),
    )
    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("remote_key", sa.String(), nullable=True),
        sa.Column("remote_url", sa.String(), nullable=True),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("build_run_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column(
            "extension_config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["build_run_id"], ["durable_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("work_items_project_idx", "work_items", ["project_id"], unique=False)
    op.create_index(
        "work_items_remote_unique",
        "work_items",
        ["source", "remote_key"],
        unique=True,
        sqlite_where=sa.text("remote_key IS NOT NULL"),
    )
    op.create_index("work_items_repo_idx", "work_items", ["repo", "pr_number"], unique=False)
    op.create_index("work_items_status_idx", "work_items", ["status"], unique=False)
    op.create_table(
        "agent_call_artifacts",
        sa.Column("agent_call_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["agent_call_id"], ["agent_calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_call_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_call_artifacts")
    op.drop_index("work_items_status_idx", table_name="work_items")
    op.drop_index("work_items_repo_idx", table_name="work_items")
    op.drop_index(
        "work_items_remote_unique",
        table_name="work_items",
        sqlite_where=sa.text("remote_key IS NOT NULL"),
    )
    op.drop_index("work_items_project_idx", table_name="work_items")
    op.drop_table("work_items")
    op.drop_table("notifications")
    op.drop_index("agent_calls_run_idx", table_name="agent_calls")
    op.drop_index("agent_calls_account_finished_idx", table_name="agent_calls")
    op.drop_table("agent_calls")
    op.drop_table("user_settings")
    op.drop_index("usage_scrapes_account_harness_time_idx", table_name="usage_scrapes")
    op.drop_table("usage_scrapes")
    op.drop_table("skills")
    op.drop_index("project_repos_project_idx", table_name="project_repos")
    op.drop_table("project_repos")
    op.drop_table("harness_logins")
    op.drop_table("durable_runs")
    op.drop_table("skill_collections")
    op.drop_table("settings_overrides")
    op.drop_table("projects")
    op.drop_table("notification_destinations")
    op.drop_table("mcp_servers")
    op.drop_table("mcp_oauth_grants")
    op.drop_table("harnesses")
    op.drop_index("events_subject_idx", table_name="events")
    op.drop_table("events")
    op.drop_table("accounts")
