from druks.workflows import Subject, SubjectSummary


class ReviewSummary(SubjectSummary):
    # A pull request keeps no row, so its id is its whole record — "owner/repo#7"
    # is where the review happened, and everything else on the row reads back out
    # of it. Its status carries the lifecycle.
    repo: str
    pr_number: int
    pull_request_url: str

    @classmethod
    def from_subject(cls, subject: Subject) -> "ReviewSummary | None":
        # Ids reach the read-side as free text off a URL, so a shape that names no
        # pull request is a miss rather than a crashed row.
        repo, _, number = subject.id.partition("#")
        owner, _, name = repo.partition("/")
        try:
            pr_number = int(number)
        except ValueError:
            return
        if owner and name and pr_number > 0:
            return cls(
                id=subject.id,
                repo=repo,
                pr_number=pr_number,
                pull_request_url=f"https://github.com/{repo}/pull/{pr_number}",
            )
        return
