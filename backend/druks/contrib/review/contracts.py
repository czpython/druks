from typing import Literal

from druks.agents import AgentOutput
from druks.contrib.ship.contracts import FindingOutput


class ReviewReport(AgentOutput):
    decision: Literal["approve", "request_changes", "comment"]
    summary: str
    findings: list[FindingOutput]
    context_repos: list[str]
