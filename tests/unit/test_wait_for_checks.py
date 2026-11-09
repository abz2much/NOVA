"""scripts/wait_for_checks.sh: the release only goes out once Validate has
passed on the merge commit. A fake `gh` on PATH plays back what GitHub says,
one answer per call, the last one repeating."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_checks.sh"


def _run(tmp_path, answers, limit="2", step="1"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "answers").write_text("\n".join(answers) + "\n")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f"n=$(cat '{tmp_path}/count' 2>/dev/null || echo 0)\n"
        f"echo $((n + 1)) > '{tmp_path}/count'\n"
        f"line=$(sed -n \"$((n + 1))p\" '{tmp_path}/answers')\n"
        f"[ -z \"$line\" ] && line=$(tail -n 1 '{tmp_path}/answers')\n"
        "echo \"$line\"\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "REPO": "owner/repo", "SHA": "abc123",
           "WAIT_LIMIT_S": limit, "WAIT_STEP_S": step}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_a_passed_run_lets_the_release_go(tmp_path):
    out = _run(tmp_path, ["completed success"])
    assert out.returncode == 0, out.stderr


def test_a_run_that_is_still_going_is_waited_for(tmp_path):
    out = _run(tmp_path, ["in_progress ", "in_progress ", "completed success"], limit="5")
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("verdict", ["failure", "cancelled", "timed_out"])
def test_a_run_that_did_not_pass_stops_the_release(tmp_path, verdict):
    out = _run(tmp_path, [f"completed {verdict}"])
    assert out.returncode == 1
    assert verdict in out.stderr


def test_no_run_at_all_stops_the_release_after_the_limit(tmp_path):
    out = _run(tmp_path, ["none"], limit="2")
    assert out.returncode == 1
    assert "did not finish" in out.stderr


def test_the_script_refuses_to_run_without_a_commit(tmp_path):
    env = {**os.environ, "REPO": "owner/repo"}
    env.pop("SHA", None)
    out = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert out.returncode != 0
