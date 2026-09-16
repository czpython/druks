## Publish the human handoff on GitHub

Use the final synthesized verdict to select the event below. `none` means the build
will rework automatically: return the full JSON result without posting a review,
inline comments, or a separate PR comment. Druks keeps that decision in the run.

| Verdict | GitHub review event |
| --- | --- |
| pass | {{ "APPROVE" if build.review_mode == "approve" else "COMMENT" }} |
| fail | {{ "none" if can_rework else ("REQUEST_CHANGES" if build.review_mode == "approve" else "COMMENT") }} |
| blocked | COMMENT |

For every other event, submit one review on {{ build.repo }} #{{ build.pr_number }}
through the connected `github` MCP server. State the verdict at the start of the
body. A blocked review explains what the operator must supply or fix; it does not
request code changes that the implementer cannot make.

Use `body` as the verification section and include the code-review section when
that lens is enabled. Attach findings with valid diff locations as inline comments.
Do not request reviewers: Druks requests the assignee when it parks for review.

A GitHub MCP error must be reported in the result. It must not change the verdict
or replace the JSON result that drives the build.
