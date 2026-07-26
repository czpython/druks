## Verification profile

{% if sandbox_env_keys -%}
**VM env** (already set on this box; do not re-export or log values):
{% for name in sandbox_env_keys -%}
- `{{ name }}`
{% endfor %}
{% endif -%}
{% if not has_commands -%}
No lint / typecheck / test / smoke commands are configured for this repo. Take verification conventions from the repo's AGENTS.md if it has one; otherwise the evaluator is acceptance-criteria-driven. Never invent commands (pytest, npm test, …) the project doesn't actually use.
{% else -%}
{% for section in sections if section.commands -%}
**{{ section.label }}:**
{% for command in section.commands -%}
- `{{ command }}`
{% endfor %}
{% endfor -%}
{% endif -%}
