from druks.agents import AgentOutput


class GistOutput(AgentOutput):
    # What the summarizer agent returns: the note it read, in one line.
    gist: str
