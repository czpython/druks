{% if build.blockers %}
## Builds on

Each ticket below blocked this one and is done or canceled, or has an open pull request. A merged pull request's change is on its repository's default branch, even when that repository is not this one. The change of an open pull request is only on its branch.

{% for blocker in build.blockers %}
{% if "branch" in blocker %}
- {{ blocker.repo }}#{{ blocker.pr_number }}: {{ blocker.title }} (open). Your branch starts from its branch `{{ blocker.branch }}`, so its change is in your checkout.
{% elif blocker.pr_number %}
- {{ blocker.repo }}#{{ blocker.pr_number }}: {{ blocker.title }}
{% else %}
- {{ blocker.ticket_key }}
{% endif %}
{% endfor %}

{% endif %}
