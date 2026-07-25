{% if build.skills %}
## Skills

Load each skill below before starting work and apply its expertise. If a skill is unavailable, continue without it.

{% for skill in build.skills %}
- `/{{ skill }}`
{% endfor %}
{% endif %}
