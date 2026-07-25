import pytest
from druks.build.contracts import RepoProfilerOutput
from druks.build.extension import Build
from druks.build.models import Project, ProjectRepo
from druks.build.policy import RepoPolicy, VerificationProfile
from druks.build.workflows import Profile
from druks.skills.datastructures import InstalledSkill
from druks.skills.models import SkillCollection


@pytest.fixture(autouse=True)
def _passthrough_step(monkeypatch, db_engine):
    # run() is itself a durable step (single-operation workflow) — route it
    # straight through so the test needs no live DBOS runtime.
    from druks.durable.engine import configure_engine

    configure_engine(db_engine)

    async def _run_step(_options, fn):
        return await fn()

    monkeypatch.setattr("druks.workflows.DBOS.run_step_async", _run_step)
    yield
    configure_engine(None)


def _seed_repo() -> ProjectRepo:
    project = Project.create(name="Acme")
    return ProjectRepo.create(project_id=project.id, full_name="acme/widget")


def _seed_skills(*names: str, disabled: tuple[str, ...] = ()) -> None:
    collection = SkillCollection.create(
        source="test",
        name="test skills",
        skills=[
            InstalledSkill(name=name, description=f"{name} skill", path=name, content_hash="x")
            for name in names
        ],
    )
    for skill in collection.skills:
        if skill.name in disabled:
            skill.enabled = False


def _profiled(**overrides) -> dict:
    profile = {
        "languages": ["python"],
        "frameworks": ["django"],
        "package_managers": ["uv"],
        "stack_summary": "A Django backend.",
        "verification": {
            "test_commands": ["pytest"],
            "lint_commands": ["ruff check ."],
            "typecheck_commands": [],
        },
        "recommended_skills": ["django-patterns"],
    }
    profile.update(overrides)
    return profile


async def _no_policy(repo):
    return RepoPolicy()


def test_profiler_output_maps_onto_the_stored_shape():
    output = RepoProfilerOutput(
        languages=["python"],
        frameworks=["django"],
        package_managers=["uv"],
        stack_summary="A Django backend.",
        test_commands=["pytest"],
        lint_commands=["ruff check ."],
        typecheck_commands=[],
        recommended_skills=["django-patterns"],
    )
    assert output.to_result() == _profiled()


@pytest.mark.parametrize("refresh_only", [False, True])
async def test_dispatch_shapes_the_profile_start(db_session, monkeypatch, refresh_only):
    repo = _seed_repo()
    calls: list[dict] = []

    async def _start(cls, **kwargs):
        calls.append(kwargs)
        return "profile-run"

    monkeypatch.setattr(Profile, "start", classmethod(_start))

    run_id = await Profile.dispatch(repo, refresh_only=refresh_only)

    assert run_id == "profile-run"
    assert calls == [
        {
            "subject": {"type": "project_repo", "id": repo.id},
            "repo_id": repo.id,
            "refresh_only": refresh_only,
        }
    ]


class TestProfileRun:
    async def test_persists_baseline_and_effective(self, db_session, monkeypatch):
        _seed_skills("django-patterns")
        repo = _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        monkeypatch.setattr(Build, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        # The step commits on its own Session; re-fetch instead of trusting
        # the identity-mapped `repo` object across that boundary.
        repo = ProjectRepo.get(repo.id)

        assert repo.profile["baseline"]["languages"] == ["python"]
        assert repo.effective_profile()["verification"]["lint_commands"] == ["ruff check ."]

    async def test_drops_skills_that_are_not_enabled(self, db_session, monkeypatch):
        _seed_skills("django-patterns", "retired-skill", disabled=("retired-skill",))
        repo = _seed_repo()

        async def _profiler(*, repo: str):
            # The agent picked a disabled skill and one that was never real.
            return _profiled(
                recommended_skills=["django-patterns", "retired-skill", "made-up-skill"]
            )

        monkeypatch.setattr(Build, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        repo = ProjectRepo.get(repo.id)

        assert repo.profile["baseline"]["recommended_skills"] == ["django-patterns"]

    async def test_pinned_verification_replaces_the_detected_one(self, db_session, monkeypatch):
        repo = _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(test_commands=("make test",)))

        monkeypatch.setattr(Build, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id)
        repo = ProjectRepo.get(repo.id)

        # The pin replaces the whole verification section on the effective profile...
        assert repo.effective_profile()["verification"]["test_commands"] == ["make test"]
        assert repo.effective_profile()["verification"]["lint_commands"] == []
        # ...but the detected baseline is preserved underneath it.
        assert repo.profile["baseline"]["verification"]["lint_commands"] == ["ruff check ."]


class TestRefreshOnly:
    async def test_skips_the_agent_and_reapplies_the_pin(self, db_session, monkeypatch):
        repo = _seed_repo()
        baseline = _profiled()
        repo.set_profile(baseline=baseline, effective=baseline)

        async def _boom(*, repo: str):
            raise AssertionError("refresh_only must not call the repo profiler")

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(test_commands=("make test",)))

        monkeypatch.setattr(Build, "repo_profiler", _boom)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id, refresh_only=True)
        repo = ProjectRepo.get(repo.id)

        # Baseline untouched — only the pin re-applies.
        assert repo.profile["baseline"]["verification"]["test_commands"] == ["pytest"]
        assert repo.effective_profile()["verification"]["test_commands"] == ["make test"]


class TestProfileStatus:
    """ProjectRepoSummary derives profile lifecycle state from the profiler's
    runs on every read — a repo with a profile is ready even if a later refresh
    failed, so there's no separate 'ready but stale' state."""

    def _summary(self, repo):
        from druks.build.schemas import ProjectRepoSummary

        return ProjectRepoSummary.from_repo(repo)

    def _seed_run(self, db_session, repo, *, state, failure=None):
        from conftest import seed_dbos_status
        from druks.durable import Run
        from uuid_utils import uuid7

        run = Run(id=str(uuid7()), kind="build.profile", failure=failure)
        db_session.add(run)
        db_session.flush()
        seed_dbos_status(db_session, run.id, state, subject={"type": "project_repo", "id": repo.id})
        return run

    def test_unprofiled_when_no_run_and_no_profile(self, db_session):
        assert self._summary(_seed_repo()).profile_status == "unprofiled"

    def test_ready_when_profiled(self, db_session):
        repo = _seed_repo()
        repo.set_profile(baseline=_profiled(), effective=_profiled())
        assert self._summary(repo).profile_status == "ready"

    def test_ready_even_when_a_later_run_failed(self, db_session):
        repo = _seed_repo()
        repo.set_profile(baseline=_profiled(), effective=_profiled())
        self._seed_run(db_session, repo, state="failed", failure="refresh boom")
        assert self._summary(repo).profile_status == "ready"

    def test_failed_when_run_failed_and_no_profile(self, db_session):
        repo = _seed_repo()
        self._seed_run(db_session, repo, state="failed", failure="boom")
        summary = self._summary(repo)
        assert summary.profile_status == "failed"
        assert summary.profiler_run_failure == "boom"

    def test_running_when_the_profiler_is_in_flight(self, db_session):
        repo = _seed_repo()
        self._seed_run(db_session, repo, state="running")
        summary = self._summary(repo)
        assert summary.profile_status == "running"
        assert summary.profiler_run_failure is None
