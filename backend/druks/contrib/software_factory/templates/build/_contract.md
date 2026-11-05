{% if build.ticket_ref %}
**MANDATORY FIRST ACTION — fetch the ticket. This is not a suggestion.** Your very first tool call MUST be to fetch `{{ build.ticket_ref }}` with your `{{ build.source }}` tools, then read its full description and **every** comment. Do this before you read the codebase, write a plan, edit a file, or emit any output. Do not begin from the ticket reference, title, or the rendered plan alone — those are derived; the ticket and its operator comments are the binding source of truth, and frequently carry exact decisions you must honor verbatim. The ONLY acceptable reason to proceed without the ticket's full text is a genuine tool failure, which you must report as a blocker — never guess or fabricate the requirements. If the source materially contradicts a plan or acceptance criteria rendered below, surface the conflict rather than silently proceeding.

{% endif %}
{% if build.journal.plan.plan_markdown %}
## Current plan

{{ build.journal.plan.plan_markdown }}

{% endif %}
{% if build.journal.plan.acceptance_criteria %}
## Acceptance criteria

{% for ac in build.journal.plan.acceptance_criteria %}
### {{ ac.id }}

**Description:** {{ ac.description }}

{% if ac.verification %}
**Verification:** {{ ac.verification }}

{% endif %}
{% endfor %}
{% endif %}
{% if build.journal.plan.rejected_approaches %}
## Ruled out

{% for approach in build.journal.plan.rejected_approaches %}
- {{ approach }}
{% endfor %}

{% endif %}
{% if build.journal.reviews %}
## Prior implementation review

{% for review in build.journal.reviews %}
### Round {{ loop.index }} — decision: {{ review.decision }}

{{ review.summary }}

{% for finding in review.findings %}
- **{{ finding.severity }}** {{ finding.summary }}{% if finding.path %} (`{{ finding.path }}{% if finding.line %}:{{ finding.line }}{% endif %}`){% endif %}: {{ finding.evidence }}
{% endfor %}

{% endfor %}
{% endif %}
