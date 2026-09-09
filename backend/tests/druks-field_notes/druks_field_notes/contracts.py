from druks.agents import AgentOutput


class GistOutput(AgentOutput):
    # What the summarizer agent returns: the note it read, in one line.
    gist: str

    def to_artifact(self) -> dict[str, str]:
        return {"kind": "markdown", "title": "Gist", "content": self.gist}

    def to_event(self) -> dict[str, str]:
        return {"topic": "gist.prepared", "summary": self.gist}
