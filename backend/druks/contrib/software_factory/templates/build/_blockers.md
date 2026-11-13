{% if build.blockers %}
## Builds on

Each ticket below blocked this one and is done or canceled. A merged pull request's change is on its repository's default branch, even when that repository is not this one.

{% for blocker in build.blockers %}
{% if blocker.pr_number %}
- {{ blocker.repo }}#{{ blocker.pr_number }}: {{ blocker.title }}
{% else %}
- {{ blocker.ticket_key }}
{% endif %}
{% endfor %}

{% endif %}
