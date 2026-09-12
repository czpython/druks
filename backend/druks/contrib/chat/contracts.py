from druks.agents import AgentOutput


class TurnOutput(AgentOutput):
    # What one chat turn returns: the assistant line to append.
    text: str
