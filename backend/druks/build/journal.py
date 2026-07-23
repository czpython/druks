from contextlib import suppress

from druks.build.contracts import (
    EvaluationOutput,
    ImplementationOutput,
    PlanData,
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
    def assignee_github_login(self) -> str | None:
        for plan in reversed(self.filter(PlanData)):
            if plan.assignee_github_login:
                return plan.assignee_github_login
        return

    @property
    def human_feedback(self) -> list[dict[str, str]]:
        # A triage digests the request_changes reply recorded just before it; the
        # newest reply has no triage yet while its own triage agent renders this
        # very projection, so it contributes no pair.
        pairs = []
        for reply in self.filter(ReviewWork, action="request_changes"):
            if triages := self.filter(TriageOutput, after=reply):
                triage = triages[0]
                pairs.append(
                    {
                        "reviewer": reply.reviewer or "(triage)",
                        "body": triage.body,
                        "question": triage.question,
                        "implementation_instructions": triage.implementation_instructions,
                    }
                )
        return pairs
