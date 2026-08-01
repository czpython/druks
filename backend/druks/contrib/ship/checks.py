from druks.doctor import CheckResult


def check_linear() -> CheckResult:
    # The Ship extension imports this module, so it can't be imported at top.
    import druks.contrib.ship.extension as ship_extension

    settings = ship_extension.Ship.settings()
    api_key = ship_extension.secret_value(settings.linear_api_key)
    if not api_key:
        return CheckResult(name="linear", ok=True, detail="not configured")
    if ship_extension.secret_value(settings.linear_webhook_secret):
        return CheckResult(name="linear", ok=True, detail="set")
    return CheckResult(
        name="linear",
        ok=False,
        detail="Linear API key set but webhook secret empty.",
    )


def check_jira() -> CheckResult:
    # The Ship extension imports this module, so it can't be imported at top.
    import druks.contrib.ship.extension as ship_extension

    settings = ship_extension.Ship.settings()
    api_token = ship_extension.secret_value(settings.jira_api_token)
    if not (settings.jira_base_url and settings.jira_email and api_token):
        return CheckResult(name="jira", ok=True, detail="not configured")
    if ship_extension.secret_value(settings.jira_webhook_secret):
        return CheckResult(name="jira", ok=True, detail="set")
    return CheckResult(
        name="jira",
        ok=False,
        detail="Jira credentials set but webhook secret empty.",
    )
