import subprocess
from pathlib import Path

import druks.sandbox

_HELPER_SCRIPT = Path(druks.sandbox.__file__).parent / "druks-sandbox.sh"


def _read_file(workspace: Path, reported: str, limit: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["sh", str(_HELPER_SCRIPT), "read-file", str(workspace), reported, str(limit)],
        capture_output=True,
        check=False,
    )


def test_read_file_streams_a_workspace_file(tmp_path):
    """The helper streams a regular in-workspace file untouched."""
    (tmp_path / "shot.png").write_bytes(b"png bytes")

    result = _read_file(tmp_path, "shot.png", 100)

    assert result.returncode == 0
    assert result.stdout == b"png bytes"


def test_read_file_rejects_a_missing_path(tmp_path):
    """The helper reports a missing workspace file without bytes."""
    result = _read_file(tmp_path, "missing.png", 100)

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"reported file is missing" in result.stderr


def test_read_file_rejects_a_symlink_escape(tmp_path):
    """A regular file reached through an escaping parent symlink is rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    result = _read_file(workspace, "linked/secret.txt", 100)

    assert result.returncode == 3
    assert result.stdout == b""
    assert b"escapes the workspace" in result.stderr


def test_read_file_rejects_an_oversized_file(tmp_path):
    """A file past the byte cap never streams."""
    (tmp_path / "large.bin").write_bytes(b"x" * (128 * 1024))

    result = _read_file(tmp_path, "large.bin", 70 * 1024)

    assert result.returncode == 4
    assert result.stdout == b""
    assert b"exceeds the 71680-byte limit" in result.stderr
