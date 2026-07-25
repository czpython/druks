{% if build.ticket_ref %}
**MANDATORY FIRST ACTION — fetch the ticket. This is not a suggestion.** Your very first tool call MUST be to fetch `{{ build.ticket_ref }}` from {{ build.source | default('the tracker', true) | capitalize }} using your available tools, then read its full description and **every** comment before you read the codebase, write a plan, edit a file, or emit any output. Do not begin from the ticket reference, title, or the rendered plan alone — those are derived; the ticket and its operator comments are the binding source of truth, and frequently carry exact decisions you must honor verbatim. The ONLY acceptable reason to proceed without the ticket's full text is a genuine tool failure, which you must report as a blocker — never guess or fabricate the requirements. If the source materially contradicts a plan or acceptance criteria rendered below, surface the conflict rather than silently proceeding.

{% endif %}
{% if build.journal.plan.plan_markdown %}
## Current plan

{{ build.journal.plan.plan_markdown }}

{% endif %}
{% if build.journal.plan.acceptance_criteria %}
## Acceptance criteria

{% for ac in build.journal.plan.acceptance_criteria %}
### {{ ac.id }}

**Description:** {{ ac.description.strip() }}

{% if ac.verification %}
**Verification:** {{ ac.verification.strip() }}

{% endif %}
{% endfor %}
{% endif %}
{% if build.journal.plan.rejected_approaches %}
## Ruled out

{% for approach in build.journal.plan.rejected_approaches %}
- {{ approach.strip() }}
{% endfor %}

{% endif %}
{% if build.journal.evaluations %}
## Prior implementation review

{% for review in build.journal.evaluations %}
### Round {{ loop.index }} — verdict: {{ review.verdict.value if review.verdict.value is defined else review.verdict }}

{% if review.body %}
{{ review.body.strip() }}

{% endif %}
{% endfor %}
{% endif %}
{% if build.journal.human_feedback %}
## Human feedback

{% for fb in build.journal.human_feedback %}
### {{ fb.reviewer }}

{% if fb.body %}
**Body:** {{ fb.body.strip() }}

{% endif %}
{% if fb.question %}
**Question:** {{ fb.question.strip() }}

{% endif %}
{% if fb.implementation_instructions %}
**Implementation instructions:** {{ fb.implementation_instructions.strip() }}

{% endif %}
{% endfor %}
{% endif %}
