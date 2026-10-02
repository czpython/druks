import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


@pytest.mark.parametrize("provider", ["docker", "exe"])
@pytest.mark.parametrize("doctor_exit", [0, 1])
def test_installer_checks_templates_after_the_services_are_ready(tmp_path, provider, doctor_exit):
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "calls.jsonl"
    install = tmp_path / "install"
    scripts = {
        "curl": "from pathlib import Path\nimport sys\nPath(sys.argv[-1]).write_text('')\n",
        "uname": "print('Darwin')\n",
        "docker": """import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ['INSTALLER_TEST_LOG']).open('a') as log:
    log.write(json.dumps(arguments) + '\\n')
if 'setup' in arguments:
    install = Path(os.environ['DRUKS_INSTALL_DIR'])
    (install / '.env').write_text(
        'DEFAULT_HOST_PROVIDER=' + os.environ['DRUKS_PROVIDER'] + '\\n'
        'DRUKS_DATA_HOST_DIR=' + str(install / 'data') + '\\n'
        'DRUKS_HARNESS_CONFIG_ROOT=' + str(install / 'harnesses') + '\\n'
    )
if '-tc' in arguments:
    print('1')
if arguments == ['compose', 'exec', '-T', 'web', 'druks', 'doctor']:
    sys.exit(int(os.environ['INSTALLER_DOCTOR_EXIT']))
""",
    }
    for name, script in scripts.items():
        command = commands / name
        command.write_text(f"#!{sys.executable}\n{script}")
        command.chmod(0o755)

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "DRUKS_INSTALL_DIR": str(install),
            "DRUKS_PROVIDER": provider,
            "INSTALLER_TEST_LOG": str(calls),
            "INSTALLER_DOCTOR_EXIT": str(doctor_exit),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == doctor_exit, result.stderr
    invoked = [json.loads(line) for line in calls.read_text().splitlines()]
    start = invoked.index(["compose", "up", "-d", "--wait"])
    assert invoked[start + 1] == ["compose", "exec", "-T", "web", "druks", "doctor"]
    assert ("Stack is up. Verify with:" in result.stdout) == (doctor_exit == 0)
