from druks.browser.constants import SESSION_SIGNED_OUT_SIGNAL
from druks.browser.models import StoredBrowserSession
from druks.signals import subscribe


@subscribe(SESSION_SIGNED_OUT_SIGNAL)
async def signed_out_session_goes_stale(*, session_name: str, **_: object) -> None:
    # A borrow bounced and the run failed; the stored login is dead, so the
    # session goes stale — the pane shows it and refuses borrows until a re-login.
    StoredBrowserSession.get_for_name(session_name).mark_stale()
