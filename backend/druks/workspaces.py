import os
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from druks.accounts.models import Account
from druks.core.apis.github import get_github_client
from druks.database import db_session
from druks.mcp import models as mcp_models
from druks.mcp import oauth
from druks.mcp.constants import TOKEN_ENV_PREFIX
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import MissingTokenError, SourceEnvVarUnsetError
from druks.mcp.helpers import get_bearer_token_env_var, get_grant_account
from druks.sandbox.datastructures import AgentResult, McpServer, RequiredMcpServer
from druks.sandbox.exceptions import ExecFailed
from druks.sandbox.layout import get_repo_root
from druks.user_settings.models import UserSettings

if TYPE_CHECKING:
    from druks.sandbox.host import Sandbox


@dataclass(frozen=True)
class Workspace:
    # What an agent runs in: the VM it abstracts. An app subclasses this and
    # overrides get_agent_run_kwargs for its project scaffolding (repo token, dirs)
    # and get_required_mcp_servers for MCP servers it credentials itself.
    sandbox: "Sandbox"

    @property
    def host_id(self) -> str:
        return self.sandbox.id

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Override to add what the agent's run needs on this workspace (github_token,
        # add_dirs). Base: pass the run's kwargs through untouched.
        return kwargs

    def get_required_mcp_servers(self) -> tuple[RequiredMcpServer, ...]:
        # Override to declare the servers this workspace requires and
        # credentials itself. Base: none.
        return ()

    async def run_agent(self, *, account_id: str | None, **kwargs: Any) -> AgentResult:
        run_kwargs = await self.with_mcp_servers(account_id, **self.get_agent_run_kwargs(**kwargs))
        # with_mcp_servers is the run's last DB read; commit so the step's
        # connection isn't held idle through the minutes the agent runs.
        await db_session().commit()
        return await self.sandbox.run_agent(**run_kwargs)

    async def with_mcp_servers(self, account_id: str | None, **kwargs: Any) -> dict[str, Any]:
        # Fold every MCP server into this call — the workspace's required
        # servers, then the operator registry's enabled entries. Each becomes a
        # wire shape on ``mcp_servers`` (url + derived env var, never the
        # token); each token rides ``extra_env`` under that var.
        required = self.get_required_mcp_servers()
        required_names = {server.name for server in required}
        if len(required_names) != len(required):
            # One config key per name in the emitted harness config — a dupe
            # would break the VM's config parse mid-run.
            raise ValueError(f"duplicate required MCP server names: {sorted(required_names)}")
        enabled = await mcp_models.McpServer.list_enabled()
        if not required and not enabled:
            return kwargs
        run_account = account_id or (await UserSettings.get()).fallback_account_id
        # ``extra_env`` may be omitted or an explicit ``None`` (both valid for the
        # underlying run_agent); treat them the same so the merge never unpacks None.
        env = dict(kwargs.get("extra_env") or {})
        wire = []
        for server in required:
            env[get_bearer_token_env_var(server.name)] = server.token
            wire.append(
                McpServer(
                    name=server.name,
                    url=server.url,
                    bearer_token_env_var=get_bearer_token_env_var(server.name),
                )
            )
        for server in enabled:
            if server["name"] in required_names:
                # A required server owns its name: the registry twin is neither
                # resolved (no raise, no env clobber) nor delivered.
                continue
            # Per-strategy bearer resolution, loud when a server can't
            # authenticate — delivery never ships a header the harness
            # can't fill.
            source = server["token_source"]
            if not source:
                # No bearer; auth, if any, rides the declared headers below.
                token = ""
            elif source == TokenSource.STATIC:
                # A stored token is ciphertext everywhere else; decrypted only
                # here, entering the run env.
                if not server["token"]:
                    raise MissingTokenError(server["name"])
                token = server["token"].decrypt()
            elif source == TokenSource.STATIC_FROM_ENV:
                token = os.environ.get(server["source_env_var"], "")
                if not token:
                    raise SourceEnvVarUnsetError(server["name"], server["source_env_var"])
            else:  # oauth
                grant_account = get_grant_account(server["identity_mode"], run_account)
                token = await oauth.get_access_token(server["name"], grant_account)
            bearer_token_env_var = ""
            if token:
                bearer_token_env_var = get_bearer_token_env_var(server["name"])
                env[bearer_token_env_var] = token
            env_headers = {}
            for index, (header, value) in enumerate(server["secret_headers"].items()):
                env_var = f"{TOKEN_ENV_PREFIX}{server['name'].upper()}_HEADER_{index}"
                env[env_var] = value
                env_headers[header] = env_var
            wire.append(
                McpServer(
                    name=server["name"],
                    url=server["url"],
                    bearer_token_env_var=bearer_token_env_var,
                    headers=dict(server["headers"]),
                    env_headers=env_headers,
                )
            )
        kwargs["mcp_servers"] = tuple(wire)
        if env:
            kwargs["extra_env"] = env
        return kwargs


@dataclass(frozen=True)
class RepoWorkspace(Workspace):
    # A VM with the target repo cloned in and a short-lived token its agent
    # pushes/reads through. Build's workspace extends this with the PR branch
    # and the github MCP token; the profiler uses it as-is.
    repo: str
    github_token: str

    @property
    def repo_path(self) -> str:
        return get_repo_root(self.sandbox.ssh_username)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["github_token"] = self.github_token
        return kwargs

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
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
        if account_id and (account := await Account.get(account_id, exclude_system=True)):
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
        result = await self.sandbox.exec(["sh", "-c", " && ".join(steps)], timeout=10.0)
        if not result.ok:
            raise ExecFailed(
                f"failed to set the workspace git identity: "
                f"exit={result.exit_code} stderr={result.stderr.strip()}",
                exit_code=result.exit_code,
            )
