"""老師主頁顯示的版號要跟 CHANGELOG.md 最上面那一版一致，回報問題時才對得上。"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_home_shows_version(A, teacher):
    assert f"v{A.APP_VERSION}" in teacher.get("/t").text


def test_version_matches_changelog(A):
    log = ROOT / "CHANGELOG.md"
    if not log.is_file():
        pytest.skip("這份程式沒有附 CHANGELOG.md")
    m = re.search(r"^## v(\d+\.\d+\.\d+)", log.read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == A.APP_VERSION
