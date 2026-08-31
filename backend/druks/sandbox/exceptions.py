class SandboxError(Exception):
    """Base for everything ``druks.sandbox`` raises out of its layer."""


class SandboxDownloadError(SandboxError):
    pass


class SetupScriptError(SandboxError):
    """A declared setup path cannot resolve to package bytes."""


class TemplateNotFound(SandboxError):
    """Drukbox has no template for a requirements hash."""


class TemplateUnavailable(SandboxError):
    """A declared template cannot be leased."""


class SandboxUnreachable(SandboxError):
    """The SSH connection to the VM cannot be (re-)established.

    Raised by the runner's tail loop after exhausting the reconnect
    window. Callers should treat this as terminal for the affected
    run — the orchestrator marks it ABANDONED and either reaps the
    host or leaves it for housekeeping.
    """


class ExecFailed(SandboxError):
    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class HostGone(SandboxError):
    """The provider says this host no longer exists.

    Raised by :meth:`druks.sandbox.client.Client.attach` when the sandbox
    SDK returns ``SandboxNotFoundError`` for a host id Druks still has
    a stale reference to (typically on agent_calls.sandbox_host_id).
    The caller should drop the dead host_id and either re-acquire
    (PR-scoped path) or surface this as a run failure (ephemeral path).

    Distinct from :class:`SandboxUnreachable` — that's "I can't reach
    the VM via SSH but the provider thinks it's there"; this is "the
    provider has told me the VM is gone". ``SandboxUnreachable`` may
    self-heal on retry, ``HostGone`` requires re-provisioning.
    """
