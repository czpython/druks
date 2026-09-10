from datetime import datetime

from druks.contrib.chat.enums import Autonomy
from druks.workflows import SubjectSummary


class ConversationSummary(SubjectSummary):
    # The conversation's domain header — title and autonomy are this app's;
    # status and the timeline come from the platform's subject read-side.
    title: str
    autonomy: Autonomy
    created_at: datetime
