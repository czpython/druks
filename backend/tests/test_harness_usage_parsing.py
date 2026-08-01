import json

from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness


def test_claude_parse_reports_the_weekly_limit_that_binds() -> None:
    """A weekly limit scoped to one model can bind well before the all-models
    one — reporting the all-models figure overstates what is left."""
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
                        "percent": 75,
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ],
            }
        )
    )

    assert parsed.ok
    assert parsed.week is not None
    assert parsed.week.percent_left == 25
    assert parsed.week.model == "Fable"


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
    assert parsed.week is not None
    assert parsed.week.percent_left == 10
    assert parsed.week.model is None


def test_claude_parse_falls_back_to_seven_day_without_limits() -> None:
    parsed = ClaudeHarness._parse_usage(
        json.dumps({"five_hour": {"utilization": 16.0}, "seven_day": {"utilization": 48.0}})
    )

    assert parsed.ok
    assert parsed.week is not None and parsed.week.percent_left == 52
    assert parsed.week.model is None


def test_codex_parse_reports_the_metered_model_that_binds() -> None:
    """Codex meters some models separately, and such a quota exhausts before
    the account-wide one."""
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
    assert parsed.week is not None
    assert parsed.week.percent_left == 20
    assert parsed.week.model == "GPT-5.3-Codex-Spark"


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
    assert parsed.week is not None and parsed.week.percent_left == 100
    assert parsed.unlimited is True


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
    assert parsed.week is not None and parsed.week.percent_left == 98


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
