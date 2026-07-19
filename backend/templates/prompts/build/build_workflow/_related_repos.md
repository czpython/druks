{% if build.related_repos %}
## Reference repositories

These related repos may hold useful build. They are NOT pre-cloned — if one is relevant to your task, clone it yourself:

```
git clone https://github.com/<full_name> {{ workspace.workspace_root }}/related/<name>
```

- Auth is already configured (git credential helper); clone the plain HTTPS URL.
- Clone only what you actually need — skip repos that aren't relevant.
- These are read-only references. Don't modify or push to them; the harness only commits to the assigned PR branch.
- If a clone fails (repo renamed, deleted, or inaccessible), carry on without it — it's lost context, not a blocker.

Repos:

{% for ref in build.related_repos %}
- `{{ ref.full_name }}`{% if ref.purpose %} — {{ ref.purpose }}{% endif %}
{% endfor %}

{% endif %}
