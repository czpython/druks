import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "druks" / "sandbox" / "druks-sandbox.sh"


def _exec_start(tmp_path: Path, *, token: str | None, cmd: list[str]) -> dict[str, str]:
    get_runs_root = tmp_path / "runs"
    get_runs_root.mkdir()
    env = {
        "HOME": str(tmp_path),
        "DRUKS_SANDBOX_RUNS_ROOT": str(get_runs_root),
        "PATH": "/usr/bin:/bin",
    }
    if token is not None:
        token_file = tmp_path / "github-token"
        token_file.write_text(token)
        env["DRUKS_GITHUB_TOKEN_FILE"] = str(token_file)

    cwd = tmp_path / "work"
    cwd.mkdir()

    result = subprocess.run(  # noqa: S603 — script + cwd are test-controlled
        ["sh", str(SCRIPT), "exec-start", "--run-id", "r1", "--cwd", str(cwd), "--", *cmd],
        env=env,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()

    run_dir = get_runs_root / "r1"
    return {p.name: p.read_text() for p in run_dir.iterdir() if p.is_file()}


def test_exec_start_exports_gh_token_when_token_file_present(tmp_path: Path):
    files = _exec_start(tmp_path, token="ghs_secret123", cmd=["env"])

    stdout = files["stdout.jsonl"]
    assert "GH_TOKEN=ghs_secret123" in stdout
    assert "GITHUB_TOKEN=ghs_secret123" in stdout


def test_exec_start_omits_gh_token_when_token_file_missing(tmp_path: Path):
    files = _exec_start(tmp_path, token=None, cmd=["env"])

    stdout = files["stdout.jsonl"]
    assert "GH_TOKEN=" not in stdout
    assert "GITHUB_TOKEN=" not in stdout


def test_exec_start_token_change_picked_up_on_next_spawn(tmp_path: Path):
    get_runs_root = tmp_path / "runs"
    get_runs_root.mkdir()
    token_file = tmp_path / "github-token"
    cwd = tmp_path / "work"
    cwd.mkdir()
    env_base = {
        "HOME": str(tmp_path),
        "DRUKS_SANDBOX_RUNS_ROOT": str(get_runs_root),
        "DRUKS_GITHUB_TOKEN_FILE": str(token_file),
        "PATH": "/usr/bin:/bin",
    }

    for run_id, token in [("r1", "tok-1"), ("r2", "tok-2")]:
        token_file.write_text(token)
        subprocess.run(  # noqa: S603
            ["sh", str(SCRIPT), "exec-start", "--run-id", run_id, "--cwd", str(cwd), "--", "env"],
            env=env_base,
            check=True,
            capture_output=True,
        )
        stdout = (get_runs_root / run_id / "stdout.jsonl").read_text()
        assert f"GH_TOKEN={token}" in stdout


def _git_credential(op: str, *, token_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT), "git-credential", op],
        input="protocol=https\nhost=github.com\n",
        capture_output=True,
        text=True,
        env={"DRUKS_GITHUB_TOKEN_FILE": str(token_file), "PATH": "/usr/bin:/bin"},
    )


def test_git_credential_get_emits_token_from_file(tmp_path: Path):
    token_file = tmp_path / "github-token"
    token_file.write_text("gho_rotatable_secret")

    proc = _git_credential("get", token_file=token_file)

    assert proc.returncode == 0
    assert "username=x-access-token" in proc.stdout
    assert "password=gho_rotatable_secret" in proc.stdout


def test_git_credential_store_and_erase_are_noops(tmp_path: Path):
    token_file = tmp_path / "github-token"
    token_file.write_text("gho_x")

    for op in ("store", "erase"):
        proc = _git_credential(op, token_file=token_file)
        assert proc.returncode == 0, op
        assert proc.stdout == "", op


def test_git_credential_get_silent_when_token_file_missing(tmp_path: Path):
    proc = _git_credential("get", token_file=tmp_path / "does-not-exist")

    assert proc.returncode == 0
    assert proc.stdout == ""
