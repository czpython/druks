{% if agent_instructions %}
{{ agent_instructions }}

{% endif %}
A person gave you this task in a chat. Do it now. Nobody answers questions while you
work, so decide on your own.

The task:

{{ instruction }}

Return what the person asked for as the `answer` field of your structured output.
Write it for the person.
