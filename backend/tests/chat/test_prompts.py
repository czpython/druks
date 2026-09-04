from druks.prompts import render_prompt


async def test_talk_prompt_carries_autonomy_bounded_history_and_mcp_discovery():
    rendered = await render_prompt(
        "chat/talk.md",
        autonomy="propose",
        messages=[
            {"role": "user", "body": "hello"},
            {"role": "assistant", "body": "hi"},
        ],
    )

    assert "Act on behalf of the signed-in operator" in rendered
    assert "list_open_subjects" in rendered
    assert "parkedAt" in rendered
    assert "Do not invent run ids" in rendered
    assert "Autonomy: propose" in rendered
    assert "user: hello" in rendered
    assert "assistant: hi" in rendered
