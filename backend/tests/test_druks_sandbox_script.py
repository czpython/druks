import subprocess
import time
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


def test_exec_kill_stops_the_cli_under_a_wrapper_that_then_finishes(tmp_path: Path):
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "r1"
    env = {
        "HOME": str(tmp_path),
        "DRUKS_SANDBOX_RUNS_ROOT": str(runs_root),
        "PATH": "/usr/bin:/bin",
    }
    # A harness wrapper: it outlives the kill to copy after its CLI exits.
    wrapper = f"trap : TERM && sleep 30; ec=$?; touch {run_dir}/copied; exit $ec"
    command = ["sh", str(SCRIPT), "exec-start", "--run-id", "r1", "--cwd", str(tmp_path)]
    # The runner starts the helper under setsid, as its own session's leader.
    helper = subprocess.Popen(  # noqa: S603 — script + cwd are test-controlled
        [*command, "--", "sh", "-c", wrapper], env=env, start_new_session=True
    )
    deadline = time.monotonic() + 5
    while not (run_dir / "pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)

    subprocess.run(  # noqa: S603 — script is test-controlled
        ["sh", str(SCRIPT), "exec-kill", "--run-id", "r1"], env=env, check=True
    )

    helper.wait(timeout=10)
    assert (run_dir / "exit_code").read_text() == "143"
    assert (run_dir / "copied").exists()
