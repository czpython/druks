from contextlib import suppress
from typing import Any

from druks.contrib.software_factory.contracts import (
    CiReport,
    ImplementationOutput,
    PlanData,
    ReviewOutput,
    ReviewReport,
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
    def reviews(self) -> list[ReviewReport]:
        return self.filter(ReviewReport)

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
    def is_ready_for_review(self) -> bool:
        """The round's review approved a head on which every check is green."""
        checks = self.latest(CiReport)
        if not checks or checks.conclusion != "green":
            return False
        reviews = self.filter(ReviewReport, after=checks)
        return bool(reviews) and reviews[-1].decision == "approve"

    @property
    def revision_request(self) -> str | None:
        # Red CI sends the round back before its review, so a review recorded after it
        # means the round moved on.
        checks = self.latest(CiReport)
        if checks and checks.conclusion == "red" and not self.filter(ReviewReport, after=checks):
            return checks.revision_request
        # The newest triage's instructions drive exactly one implement round.
        # A review recorded after that triage means the round already ran —
        # the review's findings are the current request, not these.
        triage = self.latest(TriageOutput)
        if (
            triage
            and triage.implementation_instructions
            and not self.filter(ReviewReport, after=triage)
        ):
            return triage.implementation_instructions
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
