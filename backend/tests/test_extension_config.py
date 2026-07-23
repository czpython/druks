import pytest
from conftest import make_settings
from druks.build.policy import RepoPolicy
from druks.extensions.config import resolve_extension_config
from druks.extensions.exceptions import ExtensionConfigError

REPO = "acme/widget"
ORG_DRUKS = "acme/.druks"


def _fake_fetch(monkeypatch, files: dict[tuple[str, str], str]):
    """Patch the resolver's fetch with an in-memory remote; returns the
    (repo, path) call log so tests can assert resolution order."""
    calls: list[tuple[str, str]] = []

    async def fetch(*, repo: str, path: str) -> str | None:
        calls.append((repo, path))
        return files.get((repo, path))

    monkeypatch.setattr("druks.extensions.config.fetch_file", fetch)
    return calls


class TestResolveExtensionConfig:
    async def test_only_the_repo_tier_is_fetched(self, monkeypatch):
        """No org-wide .druks tier — one repo, one file, one fetch."""
        calls = _fake_fetch(
            monkeypatch,
            {(REPO, ".druks/build/config.yml"): "sandbox:\n  image: repo-image\n"},
        )
        policy = await RepoPolicy.resolve(REPO)
        assert policy.sandbox.image == "repo-image"
        assert calls == [(REPO, ".druks/build/config.yml")]

    async def test_unknown_key_fails_loudly(self, monkeypatch):
        """A typo'd key is a ExtensionConfigError at resolution, not a silent no-op."""
        _fake_fetch(monkeypatch, {(REPO, ".druks/build/config.yml"): "verficiation: {}\n"})
        with pytest.raises(ExtensionConfigError, match="invalid build config"):
            await RepoPolicy.resolve(REPO)

    async def test_no_file_yields_defaults(self, monkeypatch):
        _fake_fetch(monkeypatch, {})
        policy = await RepoPolicy.resolve(REPO)
        assert policy == RepoPolicy()

    async def test_repo_none_skips_fetching_entirely(self, monkeypatch):
        calls = _fake_fetch(monkeypatch, {})
        await resolve_extension_config("build", repo=None, model=RepoPolicy)
        assert calls == []


class TestFetchFile:
    """The shared ``.druks`` fetcher: 404-as-empty caching, errors propagate."""

    def _wire(self, monkeypatch, tmp_path, github):
        settings = make_settings(tmp_path)
        monkeypatch.setattr("druks.extensions.fetcher.load_settings", lambda: settings)
        monkeypatch.setattr("druks.extensions.fetcher.get_github_client", lambda _settings: github)

    async def test_404_is_cached_as_empty(self, monkeypatch, tmp_path):
        from druks.extensions.fetcher import fetch_file

        fetches = []

        class _GitHub:
            async def get_file_content(self, repo, path):
                fetches.append((repo, path))
                return None

            async def aclose(self):
                pass

        self._wire(monkeypatch, tmp_path, _GitHub())
        assert await fetch_file(repo=REPO, path=".druks/build/config.yml") is None
        assert await fetch_file(repo=REPO, path=".druks/build/config.yml") is None
        assert len(fetches) == 1

    async def test_fetch_error_propagates(self, monkeypatch, tmp_path):
        from druks.extensions.fetcher import fetch_file

        class _GitHub:
            async def get_file_content(self, repo, path):
                raise RuntimeError("github down")

            async def aclose(self):
                pass

        self._wire(monkeypatch, tmp_path, _GitHub())
        with pytest.raises(RuntimeError, match="github down"):
            await fetch_file(repo=REPO, path=".druks/build/config.yml")


class TestExtensionPromptOverridePaths:
    """Unlike build's config.yml, prompt overrides keep the org-then-repo tiering."""

    async def test_repo_extension_dir_checked_before_org(self, monkeypatch):
        """``build/build_workflow/*`` resolves via the repo's
        ``.druks/build/prompts/...`` first, then the org's extension dir."""
        from druks.prompts.resolver import _resolve_override

        calls: list[tuple[str, str]] = []

        async def fetch(*, repo: str, path: str) -> str | None:
            calls.append((repo, path))
            return None

        monkeypatch.setattr("druks.prompts.resolver.fetch_file", fetch)
        body = await _resolve_override("build/build_workflow/implement.md", repo=REPO)
        assert body is None
        assert calls == [
            (REPO, ".druks/build/prompts/build_workflow/implement.md"),
            (ORG_DRUKS, "build/prompts/build_workflow/implement.md"),
        ]

    async def test_repo_extension_dir_wins(self, monkeypatch):
        from druks.prompts.resolver import _resolve_override

        async def fetch(*, repo: str, path: str) -> str | None:
            if (repo, path) == (REPO, ".druks/build/prompts/build_workflow/implement.md"):
                return "tuned"
            return None

        monkeypatch.setattr("druks.prompts.resolver.fetch_file", fetch)
        assert await _resolve_override("build/build_workflow/implement.md", repo=REPO) == "tuned"


class TestLoadPolicyAndProfile:
    """The build run resolves its policy + profile once, live — the repo's
    config plus its profiled facts. Pins ``_load_policy_and_profile`` in
    isolation; the @step runs through a pass-through so no DBOS runtime is
    needed."""

    @pytest.fixture(autouse=True)
    def _passthrough_step(self, monkeypatch, db_engine):
        from druks.durable.engine import configure_engine

        configure_engine(db_engine)

        async def _run_step(_options, fn):
            return await fn()

        monkeypatch.setattr("druks.workflows.DBOS.run_step_async", _run_step)
        yield
        configure_engine(None)

    def _flow(self, *, repo):
        from druks.build.workflows import BuildWorkflow

        flow = BuildWorkflow()
        flow.input = BuildWorkflow._run_input_model(repo=repo, pr_number=1, branch="agent/x")
        return flow

    async def test_resolves_live(self, db_session, monkeypatch):
        from druks.build.models import Project, ProjectRepo

        async def _live(cls, repo):
            return RepoPolicy.model_validate({"sandbox": {"image": "live"}})

        monkeypatch.setattr(RepoPolicy, "resolve", classmethod(_live))

        # A build only runs for a registered repo; seed it so live resolution
        # reads its (as-yet-empty) profile.
        project = Project.create(name="Acme")
        ProjectRepo.create(project_id=project.id, full_name=REPO)

        resolved = await self._flow(repo=REPO)._load_policy_and_profile()
        assert RepoPolicy.model_validate(resolved["policy"]).sandbox.image == "live"
        assert resolved["profile"] == {}  # registered but not yet profiled


class TestPolicyKeysParsing:
    async def test_full_policy_yaml_round_trips(self, monkeypatch):
        """The full policy vocabulary parses from one config.yml."""
        _fake_fetch(
            monkeypatch,
            {
                (REPO, ".druks/build/config.yml"): (
                    "gates:\n"
                    "  plan_approval: none\n"
                    "  implementation_approval: human\n"
                    "on_approval: none\n"
                    "delete_branch: false\n"
                    "verification:\n"
                    "  test_commands: [make test]\n"
                ),
            },
        )
        policy = await RepoPolicy.resolve(REPO)
        assert policy.gates.plan_approval == "none"
        assert policy.gates.implementation_approval == "human"
        assert policy.on_approval == "none"
        assert policy.delete_branch is False
        assert policy.verification.test_commands == ("make test",)

    async def test_absent_verification_key_means_no_pin(self, monkeypatch):
        _fake_fetch(monkeypatch, {(REPO, ".druks/build/config.yml"): "on_approval: none\n"})
        policy = await RepoPolicy.resolve(REPO)
        assert policy.verification is None

    async def test_unknown_gate_value_fails_loudly(self, monkeypatch):
        _fake_fetch(
            monkeypatch,
            {(REPO, ".druks/build/config.yml"): "gates: {implementation_approval: agent}\n"},
        )
        with pytest.raises(ExtensionConfigError, match="invalid build config"):
            await RepoPolicy.resolve(REPO)

    async def test_gates_default_to_inherit(self, monkeypatch):
        """Absent gates resolve via the global tier: the workflow's
        auto-dispatch setting for plan approval, human for implementation approval."""
        _fake_fetch(monkeypatch, {})
        policy = await RepoPolicy.resolve(REPO)
        assert policy.gates.plan_approval is None
        assert policy.implementation_approval_gate() == "human"
        assert policy.plan_approval_gate(auto_dispatch=True) == "none"
        assert policy.plan_approval_gate(auto_dispatch=False) == "human"
