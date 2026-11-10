import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "init_project.py"


def run(project):
    subprocess.run([sys.executable, str(SCRIPT), str(project)], check=True, capture_output=True)


def test_gitignore_created(tmp_path):
    run(tmp_path)
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines == ["logs/", ".venv/", "__pycache__/", ".env", "*.env", ".DS_Store"]


def test_gitignore_appended_and_idempotent(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n.env")
    run(tmp_path)
    run(tmp_path)
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[:2] == ["node_modules/", ".env"]
    assert lines.count(".env") == 1
    assert {"logs/", ".venv/", "__pycache__/", "*.env", ".DS_Store"} <= set(lines)
