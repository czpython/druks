import json

from druks.harnesses.codex import CodexHarness


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
