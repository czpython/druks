## Submit your review on the PR — REQUIRED (GitHub MCP)

A `github` MCP server is connected. After you decide, you **must** also submit
your review on this PR ({{ build.repo }} #{{ build.pr_number }})
through it — every time, not optionally. This is in addition to the JSON you
return: the JSON drives the build; the GitHub review is what the human sees on
the PR.

- Review event from your verdict: an **approving** verdict → `APPROVE`; a
  **changes-requested / failing** verdict → `REQUEST_CHANGES`; a **blocked /
  could-not-evaluate** verdict → skip the GitHub review. When you approve *with
  required changes*, post `APPROVE` and put the required changes in the body.
- Use your review body as the GitHub review body; attach each finding that maps
  to a file and line as an inline review comment on that line.
- Do not request reviewers on the PR — druks requests the assignee's review
  itself at the moments that await a human.

A genuine github MCP error is the only acceptable reason to skip a step — never
your own choice — and it must never change, delay, or replace the JSON verdict
you return.
