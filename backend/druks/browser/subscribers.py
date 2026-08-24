from druks.browser.constants import SESSION_SIGNED_OUT_SIGNAL
from druks.browser.enums import BrowserSessionStatus
from druks.browser.models import StoredBrowserSession
from druks.signals import subscribe


@subscribe(SESSION_SIGNED_OUT_SIGNAL)
async def signed_out_session_goes_stale(*, session_name: str, **_: object) -> None:
    # A borrow bounced and the run failed; the stored login is dead, so the
    # session goes stale — the pane shows it and refuses borrows until a re-login.
    # An anonymous session has no login to go stale: the run still fails, the
    # row stays anonymous.
    row = StoredBrowserSession.get_for_name(session_name)
    if row.status != BrowserSessionStatus.ANONYMOUS.value:
        row.mark_stale()
