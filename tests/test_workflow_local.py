"""Fast workflow checks without IntelliJ or Docker."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_editor_workflow(tmp_path):
    if not shutil.which("nvim"):
        pytest.skip("Neovim is required")
    result = subprocess.run(
        ["nvim", "--headless", "-u", "NONE", "-l", "tests/workflow.lua"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path), "NVIM_LOG_FILE": str(tmp_path / "nvim.log")},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_root_selector_runs_each_matching_subproject_once(tmp_path):
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'selector-probe'\ninclude 'a', 'b', 'a:nested'\n")
    for name in ["a", "b", "a/nested"]:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "build.gradle").write_text('''
subprojects {
    tasks.register('generateOpenApi') {
        doLast { println('BRIDGE_OPENAPI=' + project.path) }
    }
}
''')
    result = subprocess.run(
        [str(ROOT / "brain/gradlew"), "--offline", "--console=plain", "-q", "-p", str(tmp_path), "generateOpenApi"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    selected = [line for line in result.stdout.splitlines() if line.startswith("BRIDGE_OPENAPI=")]
    assert sorted(selected) == ["BRIDGE_OPENAPI=:a", "BRIDGE_OPENAPI=:a:nested", "BRIDGE_OPENAPI=:b"]
