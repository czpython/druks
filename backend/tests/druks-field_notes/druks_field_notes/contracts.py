from druks.agents import AgentOutput


class GistOutput(AgentOutput):
    # What the summarizer agent returns: the note it read, in one line.
    gist: str

    def get_artifact(self) -> dict[str, str]:
        return {"kind": "markdown", "title": "Gist", "content": self.gist}

    def get_activity(self) -> dict[str, str]:
        return {"kind": "gist.prepared", "summary": self.gist}
