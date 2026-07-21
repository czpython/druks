from dataclasses import dataclass, field

from druks.build.contracts import (
    EvaluationOutput,
    HumanFeedback,
    ImplementationOutput,
    PlanData,
    ReviewOutput,
)
from druks.build.enums import ReviewDecision


@dataclass
class PlanRecord:
    plan: PlanData
    reviews: list[ReviewOutput] = field(default_factory=list)


class BuildJournal:
    """The run's working memory, rebuilt on replay. Mutate only from workflow-body
    code after a memoized call — never inside a @step, where replay skips the
    write."""

    def __init__(self) -> None:
        self.plans: list[PlanRecord] = []
        self.implementations: list[ImplementationOutput] = []
        self.evaluations: list[EvaluationOutput] = []
        self.human_feedback: list[HumanFeedback] = []

    def add_plan(self, plan: PlanData) -> PlanData:
        self.plans.append(PlanRecord(plan=plan))
        return plan

    def add_plan_review(self, review: ReviewOutput) -> ReviewOutput:
        self.plans[-1].reviews.append(review)
        return review

    def add_implementation(self, implementation: ImplementationOutput) -> ImplementationOutput:
        self.implementations.append(implementation)
        return implementation

    def add_evaluation(self, evaluation: EvaluationOutput) -> EvaluationOutput:
        self.evaluations.append(evaluation)
        return evaluation

    def add_feedback(self, feedback: HumanFeedback) -> HumanFeedback:
        self.human_feedback.append(feedback)
        return feedback

    @property
    def plan(self) -> PlanData:
        return self.plans[-1].plan if self.plans else PlanData()

    @property
    def last_implementation(self) -> ImplementationOutput | None:
        return self.implementations[-1] if self.implementations else None

    @property
    def plan_revision(self) -> int:
        return len(self.plans)

    @property
    def implementation_revision(self) -> int:
        return len(self.implementations)

    @property
    def assignee_github_login(self) -> str | None:
        for record in reversed(self.plans):
            for review in reversed(record.reviews):
                if review.assignee_github_login:
                    return review.assignee_github_login
        return None

    def reviewer_requirements(self) -> list[ReviewOutput]:
        # Approve-with-required-changes verdicts on the current plan draft only —
        # reviews of superseded drafts don't bind the implementer.
        current = self.plans[-1].reviews if self.plans else []
        return [
            review
            for review in current
            if review.decision == ReviewDecision.APPROVE_WITH_REQUIRED_CHANGES
            and review.body.strip()
        ]
