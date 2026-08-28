from druks.prompts import render_prompt


class _Subject:
    operator = "paulo"


class _Workflow:
    # Instance access returns a coroutine, like the declared-subject descriptor.
    @property
    def subject(self):
        return self._load_subject()

    async def _load_subject(self) -> _Subject:
        return _Subject()


async def test_render_awaits_async_subject(monkeypatch):
    async def fetch_file(**_: object) -> str:
        return "operator: {{ workflow.subject.operator }}"

    monkeypatch.setattr("druks.prompts.resolver.fetch_file", fetch_file)
    rendered = await render_prompt(
        "ship/build/setup.md", repo="owner/repo", workflow=_Workflow()
    )
    assert rendered == "operator: paulo"
