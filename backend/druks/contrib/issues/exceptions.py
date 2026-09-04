class ProjectNotFound(Exception):
    def __init__(self, project_id: int) -> None:
        super().__init__(f"project {project_id} does not exist")


class InvalidPrefix(Exception):
    def __init__(self, prefix: str) -> None:
        super().__init__(f"project prefix {prefix!r} must be 2-6 letters A-Z")


class PrefixLocked(Exception):
    def __init__(self, prefix: str) -> None:
        super().__init__(
            f"project prefix {prefix!r} has already minted tickets — the identifier "
            "namespace is fixed once a number has been handed out"
        )
