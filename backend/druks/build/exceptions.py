from druks.exceptions import AgentApiError


class InvalidCursor(AgentApiError):
    code = "INVALID_CURSOR"

    def __init__(self) -> None:
        super().__init__("The cursor is not one this API issued; restart from the first page.")


class WorkItemNotFound(AgentApiError):
    status_code = 404
    code = "WORK_ITEM_NOT_FOUND"

    def __init__(self, ref: str) -> None:
        super().__init__(f"No work item {ref}.")
