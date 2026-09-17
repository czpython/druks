## Submit your review on the PR — REQUIRED (GitHub MCP)

A `github` MCP server is connected. After you decide, you **must** also submit
your review on this PR ({{ build.repo }} #{{ build.pr_number }})
through it — every time, not optionally. This is in addition to the JSON you
return: the JSON drives the build; the GitHub review is what the human sees on
the PR.

{% if build.review_mode == "approve" %}
- Review event from your verdict: an **approving** verdict → `APPROVE`; a
  **changes-requested / failing** verdict → `REQUEST_CHANGES`; a **blocked /
  could-not-evaluate** verdict → skip the GitHub review. When you approve *with
  required changes*, post `APPROVE` and put the required changes in the body.
{% else %}
- Submit every review as a `COMMENT` event — you share the identity that authored
  this PR, and GitHub refuses `APPROVE` and `REQUEST_CHANGES` from a pull
  request's author. Open the body by stating your verdict plainly ("Verdict:
  approve" / "Verdict: request changes"); a **blocked / could-not-evaluate**
  verdict → skip the GitHub review. When you approve *with required changes*,
  state the verdict and put the required changes in the body.
{% endif %}
{% if build.journal.evaluations %}
- This PR already carries your review of an earlier revision, and the reader has
  seen it. Write this review as what changed since that one, not as a full report:
  - the verdict, and one or two sentences on what this revision changed;
  - each acceptance criterion whose result changed — when none did, one line that
    says they all still pass, or names the ones that still fail;
  - the checks on the head SHA, in one line;
  - the findings that are new this round and the findings still open, one line
    each.
  Leave out the evidence for criteria whose result did not change, resolved
  findings, the round history, and every code-review finding you already posted.
  Attach an inline review comment only for a finding that is new this round.
  `body` and `review_notes` in your JSON stay complete — only the GitHub review
  is short.
{% else %}
- Use `body` as the GitHub review body{% if build.review_code %}, then append a
  `## Code review` heading and `review_notes`{% endif %}; attach each finding that
  maps to a file and line as an inline review comment on that line.
{% endif %}
- Do not request reviewers on the PR — druks requests the assignee's review
  itself at the moments that await a human.

A genuine github MCP error is the only acceptable reason to skip a step — never
your own choice — and it must never change, delay, or replace the JSON verdict
you return.
