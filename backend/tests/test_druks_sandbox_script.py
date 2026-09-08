import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "druks" / "sandbox" / "druks-sandbox.sh"


def test_exec_start_records_the_command_and_assigns_no_github_token(tmp_path: Path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    env = {
        "HOME": str(tmp_path),
        "DRUKS_SANDBOX_RUNS_ROOT": str(runs_root),
        "PATH": "/usr/bin:/bin",
    }

    result = subprocess.run(  # noqa: S603 — script + cwd are test-controlled
        ["sh", str(SCRIPT), "exec-start", "--run-id", "r1", "--cwd", str(cwd), "--", "env"],
        env=env,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode()
    files = {p.name: p.read_text() for p in (runs_root / "r1").iterdir() if p.is_file()}
    assert files["exit_code"] == "0"
    # The box's GH_TOKEN placeholder comes from Drukbox, never from this script.
    assert "GH_TOKEN=" not in files["stdout.jsonl"]
    assert "GITHUB_TOKEN=" not in files["stdout.jsonl"]
    assert "git-credential" not in SCRIPT.read_text()
