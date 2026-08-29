{% if build.skills %}
## Skills

These are installed in this VM under your agent home. Open one when its subject matter comes up in the work — not up front, and not the whole set. If a skill is unavailable, carry on without it.

{% for skill in build.skills %}
- `{{ skill.name }}` — {{ skill.description }}
{% endfor %}
{% endif %}
