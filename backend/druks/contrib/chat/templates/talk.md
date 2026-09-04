Reply to the operator.

Autonomy: {{ autonomy }}

{% for message in messages %}
{{ message.role }}: {{ message.body }}
{% endfor %}
