import json
from datetime import UTC, datetime

from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness


def test_claude_parse_keeps_every_weekly_limit_in_provider_order() -> None:
    parsed = ClaudeHarness._parse_usage(
        json.dumps(
            {
                "five_hour": {"utilization": 36.0},
                "seven_day": {"utilization": 63.0},
                "limits": [
                    {"group": "session", "percent": 36, "scope": None},
                    {"group": "weekly", "percent": 63, "scope": None},
                    {
                        "group": "weekly",
                        "percent": 100,
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ],
            }
        )
    )

    assert parsed.ok
    assert [(week.percent_left, week.model) for week in parsed.weeks] == [
        (37, None),
        (0, "Fable"),
    ]


def test_claude_parse_counts_a_limit_scoped_to_something_other_than_a_model() -> None:
    """A limit can be scoped by surface rather than model — it still counts
    toward the quota, it just has no model to name."""
    parsed = ClaudeHarness._parse_usage(
        json.dumps(
            {
                "five_hour": {"utilization": 10.0},
                "seven_day": {"utilization": 20.0},
                "limits": [
                    {"group": "weekly", "percent": 20, "scope": None},
                    {
                        "group": "weekly",
                        "percent": 90,
                        "scope": {"model": None, "surface": "code"},
                    },
                ],
            }
        )
    )

    assert parsed.ok
    assert [(week.percent_left, week.model) for week in parsed.weeks] == [
        (80, None),
        (10, None),
    ]


def test_claude_parse_falls_back_to_seven_day_without_limits() -> None:
    parsed = ClaudeHarness._parse_usage(
        json.dumps({"five_hour": {"utilization": 16.0}, "seven_day": {"utilization": 48.0}})
    )

    assert parsed.ok
    assert len(parsed.weeks) == 1
    assert parsed.weeks[0].percent_left == 52
    assert parsed.weeks[0].model is None


def test_codex_parse_keeps_every_weekly_limit_in_provider_order() -> None:
    parsed = CodexHarness._parse_usage(
        json.dumps(
            {
                "plan_type": "prolite",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 2,
                        "limit_window_seconds": 604800,
                        "reset_at": 1786173475,
                    },
                    "secondary_window": None,
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 80,
                                "limit_window_seconds": 604800,
                                "reset_at": 1786181580,
                            },
                            "secondary_window": None,
                        },
                    }
                ],
            }
        )
    )

    assert parsed.ok
    assert [(week.percent_left, week.model) for week in parsed.weeks] == [
        (98, None),
        (20, "GPT-5.3-Codex-Spark"),
    ]


def test_codex_parse_treats_unlimited_credits_as_full_buckets() -> None:
    """Business accounts carry ``rate_limit: null`` with unlimited credits —
    the expected shape for an unmetered plan, not a parse failure."""
    payload = json.dumps(
        {
            "plan_type": "business",
            "rate_limit": None,
            "credits": {"has_credits": True, "unlimited": True, "balance": None},
        }
    )

    parsed = CodexHarness._parse_usage(payload)

    assert parsed.ok
    assert parsed.plan_tier == "business"
    assert parsed.five_hour is not None and parsed.five_hour.percent_left == 100
    assert len(parsed.weeks) == 1 and parsed.weeks[0].percent_left == 100
    assert parsed.unlimited is True


def test_codex_parse_reads_a_group_spend_control_as_a_weekly_window() -> None:
    """Under group-based spend controls a business account carries no windows —
    the spend quota is the metered limit, on a cycle that runs weeks."""
    parsed = CodexHarness._parse_usage(
        json.dumps(
            {
                "plan_type": "business",
                "rate_limit": None,
                "credits": {"has_credits": True, "unlimited": False, "balance": None},
                "spend_control": {
                    "reached": False,
                    "individual_limit": {
                        "source": "group_based_spend_controls",
                        "limit": "100",
                        "used": "1.23",
                        "used_percent": 1,
                        "remaining_percent": 99,
                        "reset_after_seconds": 1681405,
                        "reset_at": 1788220800,
                    },
                },
            }
        )
    )

    assert parsed.ok
    assert parsed.plan_tier == "business"
    assert parsed.five_hour is None
    assert len(parsed.weeks) == 1
    assert parsed.weeks[0].percent_left == 99
    assert parsed.weeks[0].resets_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert parsed.unlimited is False


def test_codex_parse_places_a_weekly_only_plan_in_the_week_window() -> None:
    """A plan whose only quota is weekly reports it as the primary window."""
    parsed = CodexHarness._parse_usage(
        json.dumps(
            {
                "plan_type": "prolite",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 2,
                        "limit_window_seconds": 604800,
                        "reset_at": 1786173475,
                    },
                    "secondary_window": None,
                },
            }
        )
    )

    assert parsed.ok
    assert parsed.five_hour is None
    assert len(parsed.weeks) == 1 and parsed.weeks[0].percent_left == 98


def test_codex_parse_rejects_an_unreadable_window() -> None:
    parsed = CodexHarness._parse_usage(
        json.dumps(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 5,
                        "limit_window_seconds": "one week",
                        "reset_at": 1786173475,
                    }
                },
            }
        )
    )

    assert not parsed.ok
    assert parsed.error == "unexpected_payload"


def test_codex_parse_still_fails_without_windows_or_unlimited() -> None:
    parsed = CodexHarness._parse_usage(
        json.dumps({"plan_type": "plus", "rate_limit": None, "credits": {"unlimited": False}})
    )

    assert not parsed.ok
    assert parsed.error == "parse_failed"
