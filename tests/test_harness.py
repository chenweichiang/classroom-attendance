"""測試外殼自己的回歸。刻意不放進 mutmut 的測試選集（pyproject 的 pytest_add_cli_args_test_selection）：
這裡用子行程跑 pytest，突變版 app.py 四十幾 MB，子行程載入就逾時。"""
import subprocess, sys, textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_mutant_mode_reset_survives_repeated_pytest_main(tmp_path):
    """mutmut 在同一個行程先跑統計、乾淨測試，再分叉出每個突變的子行程，每一次都呼叫 pytest.main，
    而 pytest 9 每次都重新 import conftest、app 卻留在 sys.modules。第二輪的 A fixture 不能從上一輪留下的 app
    重拍快照：上一輪的暫存目錄被清掉後全部 setup error（突變一律記成被殺），上一輪改過的模組變數也會被當成原樣。"""
    script = textwrap.dedent("""
        import os, sys, shutil
        import pytest
        os.environ["MUTANT_UNDER_TEST"] = "stats"
        def run(n):
            return int(pytest.main(["-q", "-p", "no:randomly", "--basetemp", os.path.join(sys.argv[1], "bt%d" % n),
                                    "tests/test_api.py", "-k", "reopen_restarts_pin_fail_count or reopen_removes_auto_absent"]))
        rc1 = run(1)
        app = sys.modules["app"]
        shutil.rmtree(os.path.join(sys.argv[1], "bt1"))  # pytest 清掉上一輪的暫存目錄
        app.DISCORD_TOKEN = "left-by-previous-run"     # 上一輪的測試直接改了模組變數
        rc2 = run(2)
        print("MUTANT_RESET_RESULT", rc1, rc2, repr(app.DISCORD_TOKEN))
    """)
    r = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path)], cwd=ROOT,
                       capture_output=True, text=True, timeout=110)
    line = next((l for l in r.stdout.splitlines() if l.startswith("MUTANT_RESET_RESULT")), None)
    assert line, f"子行程沒跑完：\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    _, rc1, rc2, token = line.split(" ", 3)
    assert rc1 == "0", f"第一輪就沒過：\n{r.stdout[-2000:]}"
    assert rc2 == "0", f"第二輪 pytest.main 失敗（快照從上一輪的 app 重拍）：\n{r.stdout[-2000:]}"
    assert token == "''", f"上一輪改過的模組變數沒還原成載入時的樣子：{token}"


# Helpers.dev 的「手機」不可跨測試沿用
# 原本鍵是 (id(app), 名字)，而 importlib.reload 回傳同一個模組物件，id(app) 整個行程不變：第二個測試用同樣的裝置名，
# 拿到的是第一個測試那支手機（連 cookie 一起）。兩個測試各自呼叫 helper，先到的留下 cookie，後到的不准看到。
def _probe(h, tag):
    phone = h.dev("shared-phone-name")
    assert "dev1_mark" not in phone.cookies, "拿到了別的測試留下的 cookie：Helpers.dev 的鍵沒有隨測試重設"
    phone.cookies.set("dev1_mark", tag)


def test_dev_is_fresh_per_test_a(h):
    _probe(h, "a")


def test_dev_is_fresh_per_test_b(h):
    _probe(h, "b")


# 測試腳本不寫死 /tmp 路徑
# scenario.py 與兩支 e2e friction 原本直接存取固定的 /tmp/attend-*.db：兩個閘門（或閘門與手動重跑）同時跑會互踩同一個庫。
# 改成環境變數（ATTEND_SCEN_DB、ATTEND_E2E_DB）；沒給時用 tempfile.mkdtemp()。
_FIXED = "/tmp/" + "attend-"  # 分開寫，免得這支檔案自己被下面的掃描抓到


def test_no_hardcoded_tmp_attend_paths_in_test_scripts():
    bad = []
    for p in sorted((ROOT / "tests").glob("*.py")):
        if p.name == Path(__file__).name:  # 自己的錯誤訊息裡有這個字串
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _FIXED in line and not line.lstrip().startswith("#"):
                bad.append(f"{p.name}:{i}")
    assert not bad, "測試腳本裡有寫死的 /tmp/attend- 路徑（要改成環境變數或 tempfile.mkdtemp）：" + ", ".join(bad)


def test_gate_passes_unique_db_paths_under_logdir():
    gate = (ROOT / "tests" / "gate.sh").read_text(encoding="utf-8")
    assert 'ATTEND_SCEN_DB="$LOGDIR/' in gate, "gate.sh 的 scenario 層沒有傳 ATTEND_SCEN_DB"
    assert gate.count('ATTEND_E2E_DB="$dbfile"') >= 2, "gate.sh 的 e2e-friction／e2e-teacher 兩層都要傳 ATTEND_E2E_DB"


def test_scenario_uses_env_path_or_a_fresh_tempdir(tmp_path):
    import os
    fixed = Path(_FIXED + "scen.db")
    before = fixed.stat().st_mtime_ns if fixed.exists() else None
    mine = tmp_path / "scen-mine.db"
    env = {**os.environ, "ATTEND_SCEN_DB": str(mine)}
    r = subprocess.run([sys.executable, "-B", "tests/scenario.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=100)
    assert r.returncode == 0 and mine.exists(), (r.stdout[-800:], r.stderr[-800:])
    # 沒給環境變數：落在 tempfile.mkdtemp()（TMPDIR 指到 tmp_path 好檢查），不碰固定路徑
    tdir = tmp_path / "tmpdir"; tdir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "ATTEND_SCEN_DB"} | {"TMPDIR": str(tdir)}
    r = subprocess.run([sys.executable, "-B", "tests/scenario.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=100)
    assert r.returncode == 0, (r.stdout[-800:], r.stderr[-800:])
    assert list(tdir.rglob("*.db")), "沒給 ATTEND_SCEN_DB 時，資料庫應該建在 mkdtemp 目錄底下"
    assert (fixed.stat().st_mtime_ns if fixed.exists() else None) == before, "固定路徑的庫被動到了"


# Discord 背景執行緒不得活過自己的測試
# 偶發 ERROR 的成因：結束點名起的背景執行緒（daemon、沒人 join）在下一個測試 reload 之後才跑，
# 寫進新庫或在新庫切 WAL 時撞 `database is locked`。探針在 tests/probe_bg_thread.py，這裡依序指名跑。
def _run_probe(*nodes, bg_join="5"):
    import os
    env = {**os.environ, "ATTEND_TEST_BG_JOIN": bg_join}
    return subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", "--no-header",
                           *[f"tests/probe_bg_thread.py::{n}" for n in nodes]],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=100)


def test_next_test_does_not_see_previous_tests_discord_thread():
    r = _run_probe("test_a_spawns_slow_background_post", "test_b_starts_clean")
    assert r.returncode == 0, "上一個測試的 Discord 背景執行緒活過了自己的測試：\n" + r.stdout[-1500:]


def test_stuck_background_thread_fails_the_test_and_names_the_thread():
    r = _run_probe("test_c_stuck_background_post", "test_zz_release", bg_join="1")
    assert r.returncode != 0, "背景執行緒卡住不結束，測試卻過了\n" + r.stdout[-1500:]
    out = r.stdout
    assert "Discord 背景執行緒" in out and "沒有結束" in out and "Thread-" in out, out[-1500:]
    assert "test_c_stuck_background_post" in out, out[-1500:]  # 失敗算在起執行緒的那個測試頭上


def _gate_layer_body(name):
    import re
    text = (ROOT / "tests" / "gate.sh").read_text(encoding="utf-8")
    m = re.search(rf"^layer_{name}\(\) \{{\n(.*?)^\}}\n", text, re.S | re.M)
    assert m, f"gate.sh 找不到 layer_{name}"
    return m.group(1)


def test_flaky_layer_also_repeats_teacher_friction_tests():
    """偶發 ERROR 都出在 test_teacher_friction.py，flaky 層只重跑 test_api.py 抓不到。"""
    body = _gate_layer_body("flaky")
    assert "tests/test_api.py" in body and "tests/test_teacher_friction.py" in body, body


def test_new_test_files_are_in_the_unit_layer():
    body = _gate_layer_body("unit")
    for f in ("tests/test_manage.py", "tests/test_fake_names.py"):
        assert f in body, f"unit 層沒有跑 {f}"


def test_gate_rehearsal_layer_runs_two_seeds_and_fails_if_either_fails():
    """彩排層要跑兩組種子（不給 --seed 時預設只跑種子 1），任一組沒過這一層就失敗。"""
    body = _gate_layer_body("rehearsal")
    assert "for seed in 1 2" in body and '--seed "$seed"' in body, body
    assert "rc=1" in body and "return $rc" in body, "任一組種子沒過，這一層就要失敗"
