import asyncio
import hashlib
import mimetypes
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from druks.accounts.models import Account
from druks.core.apis.github import get_github_client
from druks.core.models import uuid7_str
from druks.core.services import Github
from druks.database import db_session
from druks.files.constants import MAX_FILE_BYTES
from druks.files.datastructures import File
from druks.files.exceptions import FileUnavailableError
from druks.files.models import FileRecord
from druks.files.storage import get_file_storage
from druks.mcp import models as mcp_models
from druks.mcp import oauth
from druks.mcp.constants import TOKEN_ENV_PREFIX
from druks.mcp.enums import IdentityMode, TokenSource
from druks.mcp.exceptions import MissingGrantError, MissingTokenError
from druks.mcp.helpers import get_bearer_token_env_var, get_grant_account
from druks.sandbox import repo as checkout
from druks.sandbox.datastructures import AgentResult, McpServer, RequiredMcpServer
from druks.sandbox.exceptions import ExecFailed
from druks.sandbox.layout import get_repo_root, get_work_root
from druks.sandbox.models import SecretRef

if TYPE_CHECKING:
    from druks.sandbox.host import Host


@dataclass(frozen=True)
class Workspace:
    # What an agent runs in: the VM it abstracts.
    host: "Host"
    # What the run is about; None for a workflow about nothing.
    subject: Any = None

    @property
    def host_id(self) -> str:
        return self.host.id

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Override to add what the run needs on this workspace (add_dirs, skills).
        return kwargs

    @classmethod
    async def get_required_mcp_servers(cls, subject: Any) -> tuple[RequiredMcpServer, ...]:
        # Override to declare the servers this workspace requires and the vault
        # row each one issues through. Read before the box exists. Base: none.
        return ()

    @classmethod
    async def get_secret_refs(cls, subject: Any) -> list[SecretRef]:
        # The secrets a box of this workspace fetches, beyond its profile's.
        # Read before the box exists, so from the subject alone. Base: none.
        return []

    async def prepare_context(
        self, context: dict[str, Any], *, agent_call_id: str
    ) -> dict[str, Any]:
        # Every File in the call's kwargs lands in the VM and reads as its
        # in-VM path; a file arrives as a File or inside a list, never buried
        # in a nested structure.
        prepared: dict[str, Any] = {}
        for key, value in context.items():
            if type(value) is File:
                prepared[key] = await self._upload_input_file(value, agent_call_id)
            elif type(value) is list:
                prepared[key] = [
                    await self._upload_input_file(item, agent_call_id)
                    if type(item) is File
                    else item
                    for item in value
                ]
            else:
                prepared[key] = value
        return prepared

    async def save_files(self, files: list[File], *, app: str, agent_call_id: str) -> None:
        storage = get_file_storage()
        staged: list[tuple[File, FileRecord, Path]] = []
        try:
            for file in files:
                record, temp = await self._pull_output_file(
                    file, app=app, agent_call_id=agent_call_id
                )
                staged.append((file, record, temp))
            db_session().add_all([record for _, record, _ in staged])
            await db_session().flush()
            for file, record, temp in staged:
                storage.save(temp, record.id)
                file._hydrate(record)
        except BaseException:
            for _, record, temp in staged:
                storage.discard(temp)
                storage.delete(record.id)
            raise

    async def _pull_output_file(
        self, file: File, *, app: str, agent_call_id: str
    ) -> tuple[FileRecord, Path]:
        reported = file.path
        name = PurePosixPath(reported).name
        file_id = uuid7_str()
        temp = get_file_storage().new_temp(file_id)
        try:
            await self.host.download(
                remote=reported,
                local=temp,
                workspace_root=get_work_root(self.host.ssh_username),
                max_bytes=MAX_FILE_BYTES,
            )
            sha256 = await asyncio.to_thread(_file_sha256, temp)
        except BaseException:
            get_file_storage().discard(temp)
            raise
        record = FileRecord(
            id=file_id,
            name=name,
            size=temp.stat().st_size,
            content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            sha256=sha256,
            app=app,
            agent_call_id=agent_call_id,
        )
        return record, temp

    async def _upload_input_file(self, file: File, agent_call_id: str) -> str:
        record = await db_session().get(FileRecord, file.id)
        if not record or record.deleted_at:
            raise FileUnavailableError(f"file {file.id} is deleted or missing")
        source = get_file_storage().path(file.id)
        if not source.is_file():
            raise FileUnavailableError(f"file {file.id} content is missing")
        workspace_root = get_work_root(self.host.ssh_username)
        remote = f"{workspace_root}/.druks-files/{agent_call_id}/{file.id}/{record.name}"
        await self.host.upload_file(local=source, remote=remote)
        return remote

    async def run_agent(self, *, account_id: str | None, **kwargs: Any) -> AgentResult:
        run_kwargs = await self.with_mcp_servers(account_id, **self.get_agent_run_kwargs(**kwargs))
        # with_mcp_servers is the run's last DB read; commit so the step's
        # connection isn't held idle through the minutes the agent runs.
        await db_session().commit()
        return await self.host.run_agent(**run_kwargs)

    async def with_mcp_servers(self, account_id: str | None, **kwargs: Any) -> dict[str, Any]:
        # The harness names each server's url, variables, and plain headers.
        # Every credential is a box entry, so nothing rides ``extra_env``.
        wire, _ = await self.get_mcp_delivery(self.subject, account_id)
        if wire:
            kwargs["mcp_servers"] = wire
        return kwargs

    @classmethod
    async def get_mcp_delivery(
        cls, subject: Any, account_id: str | None
    ) -> tuple[tuple[McpServer, ...], list[SecretRef]]:
        """The MCP servers a box of this workspace reaches: the wire shapes for
        the harness and the secret refs for the box's entries, one per bearer
        and per secret header. The workspace's required servers come first
        and own their names: a same-named registry entry is neither resolved
        nor delivered. A server that cannot authenticate fails here, before
        the box."""
        required = await cls.get_required_mcp_servers(subject)
        required_names = {server.name for server in required}
        if len(required_names) != len(required):
            # One config key per name in the emitted harness config — a dupe
            # would break the VM's config parse mid-run.
            raise ValueError(f"duplicate required MCP server names: {sorted(required_names)}")
        wire = []
        refs = []
        for server in required:
            variable = get_bearer_token_env_var(server.name)
            wire.append(McpServer(name=server.name, url=server.url, bearer_token_env_var=variable))
            refs.append(
                SecretRef(
                    name=variable.lower(),
                    secret_id=server.secret_id,
                    resource=server.resource,
                    host=urlsplit(server.url).hostname,
                )
            )
        run_account = account_id
        for server in await mcp_models.McpServer.list_enabled():
            name = server["name"]
            if name in required_names:
                continue
            host = urlsplit(server["url"]).hostname
            # The bearer's vault row, by source, loud when the server cannot
            # authenticate. A bearerless server rides its declared headers.
            source = server["token_source"]
            bearer_token_env_var = ""
            if source == TokenSource.STATIC:
                secret = server["token"]
                if not secret:
                    raise MissingTokenError(name)
            elif source:
                if server["identity_mode"] == IdentityMode.PER_USER and not run_account:
                    account = await Account.get_default()
                    run_account = account.id if account else None
                grant_account = get_grant_account(server["identity_mode"], run_account)
                secret = await oauth.get_connection(name, grant_account)
                if not secret:
                    raise MissingGrantError(name, grant_account)
            if source:
                bearer_token_env_var = get_bearer_token_env_var(name)
                refs.append(
                    SecretRef(name=bearer_token_env_var.lower(), secret_id=secret.id, host=host)
                )
            env_headers = {}
            for index, (header, secret) in enumerate(server["secret_headers"].items()):
                variable = f"{TOKEN_ENV_PREFIX}{name.upper()}_HEADER_{index}"
                env_headers[header] = variable
                refs.append(SecretRef(name=variable.lower(), secret_id=secret.id, host=host))
            wire.append(
                McpServer(
                    name=name,
                    url=server["url"],
                    bearer_token_env_var=bearer_token_env_var,
                    headers=dict(server["headers"]),
                    env_headers=env_headers,
                )
            )
        return tuple(wire), refs


@dataclass(frozen=True)
class RepoWorkspace(Workspace):
    """A VM with the subject's ``repo`` cloned at ``branch`` (default branch when
    None), re-cloned before every agent call. The box holds a placeholder in
    ``GH_TOKEN``, and Drukbox points git and ``gh`` at it; the Druks issuer
    answers the token of the GitHub identity this workspace names."""

    branch: str | None = None
    # The GitHub identity the box's git and gh act as: a connected service.
    github: ClassVar[type[Github]] = Github

    @classmethod
    def get_repo(cls, subject: Any) -> str:
        # Override when the subject names its ``owner/name`` differently.
        return subject.repo

    @classmethod
    async def get_secret_refs(cls, subject: Any) -> list[SecretRef]:
        # The identity's vault row and the repo: the whole selection the
        # issuer reads. A service that is not connected fails here, before the box.
        return [
            SecretRef(
                name=cls.github.secret_name,
                secret_id=(await cls.github.get()).id,
                resource=cls.get_repo(subject),
            )
        ]

    @property
    def repo_path(self) -> str:
        return get_repo_root(self.host.ssh_username)

    async def run_agent(self, *, account_id: str | None, **kwargs: Any) -> AgentResult:
        await checkout.ensure(
            self.host,
            repo_url=f"https://github.com/{self.get_repo(self.subject)}",
            ref=self.branch,
            target_path=self.repo_path,
        )
        await self.set_git_identity(account_id)
        return await super().run_agent(account_id=account_id, **kwargs)

    async def set_git_identity(self, account_id: str | None) -> None:
        """Commits in the repo are authored as the operator's bot user, with a
        prepare-commit-msg hook crediting the account that dispatched the run.
        Rewritten before every agent call so a reused warm host follows the
        current run's dispatcher — a system dispatch carries no hook and
        credits nobody."""
        author_name, author_email = await (await get_github_client()).get_bot_git_author()
        steps = [
            f"cd {shlex.quote(self.repo_path)}",
            f"git config user.name {shlex.quote(author_name)}",
            f"git config user.email {shlex.quote(author_email)}",
            "rm -f .git/hooks/prepare-commit-msg",
        ]
        if account_id and (account := await Account.get(account_id)):
            trailer = f"Co-Authored-By: {account.username} <{account.username}>"
            hook = (
                "#!/bin/sh\n"
                f"git interpret-trailers --in-place --if-exists doNothing"
                f' --trailer {shlex.quote(trailer)} "$1"\n'
            )
            steps += [
                f"printf %s {shlex.quote(hook)} > .git/hooks/prepare-commit-msg",
                "chmod 755 .git/hooks/prepare-commit-msg",
            ]
        result = await self.host.exec(["sh", "-c", " && ".join(steps)], timeout=10.0)
        if not result.ok:
            raise ExecFailed(
                f"failed to set the workspace git identity: "
                f"exit={result.exit_code} stderr={result.stderr.strip()}",
                exit_code=result.exit_code,
            )


def _file_sha256(path: Path) -> str:
    with path.open("rb") as content:
        return hashlib.file_digest(content, "sha256").hexdigest()
