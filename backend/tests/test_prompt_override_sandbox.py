import pytest
from druks.prompts import render_prompt
from jinja2.exceptions import SecurityError


async def _serve_override(body: str, monkeypatch) -> None:
    # A repo override is fetched from the monitored repo; serve one directly.
    async def fetch_file(**_: object) -> str:
        return body

    monkeypatch.setattr("druks.prompts.resolver.fetch_file", fetch_file)


async def test_override_cannot_reach_globals(monkeypatch):
    # The RCE: an override walking __globals__ to os.system. The sandbox stops it.
    await _serve_override(
        "{{ self.__init__.__globals__['os'].system('touch /tmp/pwned') }}", monkeypatch
    )
    with pytest.raises(SecurityError):
        await render_prompt("ship/build/setup.md", repo="owner/repo")


async def test_override_cannot_mutate_context(monkeypatch):
    # Immutable sandbox: an override can't mutate a live object from context either.
    await _serve_override("{{ items.append(1) }}", monkeypatch)
    with pytest.raises(SecurityError):
        await render_prompt("ship/build/setup.md", repo="owner/repo", items=[])
