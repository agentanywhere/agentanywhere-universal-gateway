"""scripts/generate_secret_key.py writes SECRET_KEY, and ADMIN_TOKEN with --admin-token."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generate_secret_key.py"
ENV_EXAMPLE = Path(__file__).resolve().parent.parent / "env.example"


def _run(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=tmp_path, capture_output=True, text=True, check=True
    )


def _values(tmp_path):
    out = {}
    for line in (tmp_path / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            name, _, value = line.partition("=")
            out[name] = value
    return out


def test_update_env_sets_only_the_secret_key(tmp_path):
    (tmp_path / ".env").write_text(ENV_EXAMPLE.read_text())
    _run(tmp_path, "--update-env")
    values = _values(tmp_path)
    assert not values["SECRET_KEY"].startswith("change-me")
    assert values["ADMIN_TOKEN"] == ""


def test_admin_token_flag_sets_both_without_duplicating_the_line(tmp_path):
    (tmp_path / ".env").write_text(ENV_EXAMPLE.read_text())
    _run(tmp_path, "--update-env", "--admin-token")
    values = _values(tmp_path)
    assert len(values["ADMIN_TOKEN"]) >= 43 and values["ADMIN_TOKEN"] != values["SECRET_KEY"]
    assert (tmp_path / ".env").read_text().count("\nADMIN_TOKEN=") == 1


def test_creates_the_env_file_when_missing(tmp_path):
    _run(tmp_path, "--update-env", "--admin-token")
    values = _values(tmp_path)
    assert values["SECRET_KEY"] and values["ADMIN_TOKEN"]


def test_without_flags_prints_a_key_and_writes_nothing(tmp_path):
    out = _run(tmp_path)
    assert len(out.stdout.strip().splitlines()[0]) >= 64
    assert not (tmp_path / ".env").exists()
