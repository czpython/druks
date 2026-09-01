from contextlib import suppress
from typing import Any

from druks.contrib.software_factory.contracts import (
    EvaluationOutput,
    ImplementationOutput,
    PlanData,
    ReviewOutput,
    ReviewWork,
    TriageOutput,
)
from druks.workflows import Journal


class BuildJournal(Journal):
    @property
    def plan(self) -> PlanData:
        return self.latest(PlanData) or PlanData()

    @property
    def plan_revision(self) -> int:
        return len(self.filter(PlanData))

    @property
    def implementations(self) -> list[ImplementationOutput]:
        return self.filter(ImplementationOutput, status="success")

    @property
    def pr_base_sha(self) -> str | None:
        with suppress(IndexError):
            return self.implementations[0].base_sha
        return

    @property
    def last_implementation(self) -> ImplementationOutput | None:
        with suppress(IndexError):
            return self.implementations[-1]
        return

    @property
    def implementation_revision(self) -> int:
        return len(self.implementations)

    @property
    def evaluations(self) -> list[EvaluationOutput]:
        return self.filter(EvaluationOutput)

    @property
    def plan_reviews(self) -> list[ReviewOutput]:
        return self.filter(ReviewOutput)

    @property
    def assignee_github_login(self) -> str | None:
        for plan in reversed(self.filter(PlanData)):
            if plan.assignee_github_login:
                return plan.assignee_github_login
        return

    @property
    def human_feedback(self) -> list[dict[str, Any]]:
        # Every request_changes reply, in order, with the triage that digested it.
        # The newest reply has no triage yet while its own triage agent renders
        # this very projection — it renders as the pending entry, so the triage
        # agent reads the reviewer's actual words, never a stale digest.
        entries = []
        for reply in self.filter(ReviewWork, action="request_changes"):
            triages = self.filter(TriageOutput, after=reply)
            entries.append(
                {
                    "reviewer": reply.reviewer,
                    "body": reply.body,
                    "triage": triages[0] if triages else None,
                }
            )
        return entries
