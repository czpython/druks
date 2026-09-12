from types import SimpleNamespace
from unittest.mock import AsyncMock

from druks.accounts.models import Account
from druks.contrib.chat.enums import Autonomy
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import Talk
from druks.workspaces import OperatorWrites


class _FakeHost:
    ssh_username = "exedev"

    def __init__(self, provider: str = "docker"):
        self.record = SimpleNamespace(provider=provider)
        self.run_agent = AsyncMock(return_value="ok")


async def test_talk_reads_live_autonomy_for_the_next_call(druks_db):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    flow = Talk()
    flow.subject = conversation
    flow.account_id = account.id
    flow._workflow_id = "run-1"

    kwargs = await flow.get_workspace_kwargs(_FakeHost())  # type: ignore[arg-type]
    assert kwargs["operator_writes"] == OperatorWrites.DENY
    assert kwargs["run_id"] == "run-1"

    conversation.autonomy = Autonomy.FULL
    kwargs = await flow.get_workspace_kwargs(_FakeHost())  # type: ignore[arg-type]
    assert kwargs["operator_writes"] == OperatorWrites.ALLOW
