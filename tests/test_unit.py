"""純函式與邊界：token、時間片、IP、CSV、Discord 切段、設定合併、自動結案冪等。"""
import importlib
import os
import sqlite3
import time
from hypothesis import HealthCheck, given, settings, strategies as st


def test_token_roundtrip_and_tamper(A):
    tok = A.make_token("g", "1", "abc", exp=time.time() + 60)
    assert A.read_token(tok, 3) == ["g", "1", "abc"]
    assert A.read_token(tok, 2) is None
    assert A.read_token(tok[:-1] + ("0" if tok[-1] != "0" else "1"), 3) is None
    assert A.read_token("garbage", 3) is None
    assert A.read_token(A.make_token("g", "1", "x", exp=time.time() - 1), 3) is None


@given(st.text(min_size=0, max_size=40))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_read_token_never_raises(A, s):
    assert A.read_token(s, 2) is None  # 隨機字串不可能過簽章


@given(st.floats(min_value=1_600_000_000, max_value=2_000_000_000), st.integers(5, 60), st.integers(0, 150))
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_current_slots_boundary(A, t, iv, grace):
    # B015：grace 不再夾在 qr_interval 以內，斷言改成新的上限語意——往前第 j 片在「該片結束後
    # grace 秒內」仍有效，grace 可以跨好幾片；一旦某片不合格，更舊的片子也不該出現（測試依據：
    # app.py current_slots 文件字串「該片結束後 grace 秒內仍算數」，非照抄實作行數）。
    real_now = A.now
    A.now = lambda: t
    try:
        sess = {"id": 1, "secret": "s"}
        slots = A.current_slots(sess, {"qr_interval": iv, "qr_grace": grace})
    finally:
        A.now = real_now
    cur = int(t // iv)
    assert slots[0] == cur
    j = 1
    while True:
        prev = cur - j
        if prev < 0:
            break
        slot_end = (prev + 1) * iv
        should_include = t - slot_end < grace
        assert (prev in slots) == should_include, (
            f"current_slots 邊界錯：iv={iv} grace={grace} t={t} prev={prev} "
            f"預期{'在' if should_include else '不在'} slots={slots}")
        if not should_include:
            break
        j += 1
    assert len(A.code6(A.slot_sig(sess, cur))) == 6 and A.code6(A.slot_sig(sess, cur)).isdigit()


def test_client_ip_trust(A):
    class R:
        def __init__(self, h, host="10.0.0.5"):
            self.headers = h
            self.client = type("c", (), {"host": host})()
    assert A.client_ip(R({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "<img>, 1.1.1.1"})) == "203.0.113.7"
    assert A.client_ip(R({"x-forwarded-for": "<img src=x>, 198.51.100.9"})) == "198.51.100.9"
    assert A.client_ip(R({"x-forwarded-for": "<img src=x>"})) == "10.0.0.5"
    assert A.client_ip(R({"cf-connecting-ip": "not-an-ip"}, host="::1")) == "::1"
    assert A.client_ip(R({"x-forwarded-for": "1.2.3.4, 140.131.1.1"})) == "140.131.1.1"   # 信最後一段（Caddy 附加的）
    assert A.client_ip(R({"x-forwarded-for": "140.131.1.1, 1.2.3.4"})) == "1.2.3.4"
    assert A.client_ip(R({"x-forwarded-for": "::1, 2001:db8::1"})) == "2001:db8::1"


def test_csv_safe(A):
    assert A.csv_safe("=1+1") == "'=1+1" and A.csv_safe("+x") == "'+x" and A.csv_safe("王小明") == "王小明" and A.csv_safe("") == ""


def test_discord_chunks(A, monkeypatch):
    sent = []
    monkeypatch.setattr(A, "discord_post", lambda ch, c: (sent.append(c) or (True, "HTTP 200")))
    A.DISCORD_TOKEN = "x"
    ok, _ = A.discord_post_long("chan", "X" * 2500 + "\nline2\n" + "\n".join(f"row{i}" for i in range(500)))
    assert ok and all(0 < len(c) <= 1900 for c in sent) and sum(len(c) for c in sent) >= 2500


def test_settings_merge_and_defaults(A):
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name,settings) VALUES('X','x','{\"grant_seconds\": 600, \"bogus\": 1}')")
        c = A.get_course(con, 1)
    st = A.course_settings(c)
    assert st["grant_seconds"] == 600 and st["open_minutes"] == 3 and st["bogus"] == 1
    with A.db() as con:
        con.execute("UPDATE courses SET settings='not json' WHERE id=1")
        assert A.course_settings(A.get_course(con, 1))["open_minutes"] == 3


def test_finalize_expired_idempotent(A):
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('X','x')")
        for i in range(3):
            con.execute("INSERT INTO students(course_id,student_id,name) VALUES(1,?,?)", (f"S{i}", f"n{i}"))
        t = A.now()
        con.execute("INSERT INTO sessions(course_id,secret,opened_at,open_until,late_until) VALUES(1,'s',?,?,?)", (t - 2000, t - 1900, t - 1000))
        A.finalize_expired(con); A.finalize_expired(con)
        assert con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=1").fetchone()[0] == 3
        assert con.execute("SELECT closed_at FROM sessions WHERE id=1").fetchone()[0] == t - 1000


def test_fmt_timezone(A):
    assert A.fmt(1, True) == "1970-01-01 08:00" and A.fmt(None) == "" and A.fmt(0) == ""


def test_discord_post_network_error(A, monkeypatch):
    import urllib.request
    A.DISCORD_TOKEN = "x"
    assert A.discord_post("", "hi")[0] is False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    ok, msg = A.discord_post("1", "hi"); assert ok is False and "boom" in msg


def test_grant_and_code_helpers(A):
    dh16 = A.hash_device("some-device-key")[:16]
    g = A.grant_for(7, {"grant_seconds": 60}, dh16)
    # A3/B029：grant 多帶一段發證時間；C-S5：再多帶一段裝置雜湊前 16 碼，nparts 從 3 變 4 再變 5
    # （g, sid, nonce, issued, dh16）
    parts = A.read_token(g, 5)
    assert parts[:2] == ["g", "7"] and parts[4] == dh16
    assert A.grant_for(7, {"grant_seconds": 60}, dh16) != g
    assert A.norm_name(" 王 小明 ") == "王小明" and A.hash_device(" k ") == A.hash_device("k")


def test_C_S8_grant_seconds_clamped_30_to_900(A):
    """C-S8（SPEC 2026-09-21）：grants 表一小時清除，憑證不該活得比它久，grant_seconds 夾在 30–900 秒。"""
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name,settings) VALUES('Y','y','{\"grant_seconds\": 5000}')")
        c_over = A.get_course(con, con.execute("SELECT id FROM courses WHERE code='Y'").fetchone()[0])
        con.execute("INSERT INTO courses(code,name,settings) VALUES('Z','z','{\"grant_seconds\": 1}')")
        c_under = A.get_course(con, con.execute("SELECT id FROM courses WHERE code='Z'").fetchone()[0])
    assert A.course_settings(c_over)["grant_seconds"] == 900
    assert A.course_settings(c_under)["grant_seconds"] == 30


def test_C_S7_migrate_legacy_cookie_reissues_new_name_and_deletes_old(A, monkeypatch):
    """C-S7：https 模式下，這次回應是靠舊名 attend_dk 才認出裝置的（新名讀不到、舊名剛好等於
    解出的值），要補發新名 cookie 並刪掉舊名，之後請求就只剩新名。"""
    from starlette.responses import Response

    class _FR:
        def __init__(self, cookies):
            self.cookies = cookies

    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    legacy = "boundoldname0000000000002"
    req = _FR({"attend_dk": legacy})
    out = A._migrate_legacy_cookie(req, Response(), legacy)
    set_cookie_vals = [v.decode() for k, v in out.raw_headers if k == b"set-cookie"]
    assert any(v.startswith("__Host-attend_dk=" + legacy) for v in set_cookie_vals), set_cookie_vals
    assert any(v.startswith("attend_dk=") and ("Max-Age=0" in v or "01 Jan 1970" in v) for v in set_cookie_vals), set_cookie_vals


def test_C_S7_effective_device_key_legacy_paths(A, monkeypatch):
    """C-S7：effective_device_key 的舊名分支——格式不合法忽略、已綁採用（con 有給就沿用它、
    沒給就臨時開一條）、未綁忽略。四條分支都要在被算進覆蓋率的檔案裡至少踩過一次。"""
    class _FR:
        def __init__(self, cookies):
            self.cookies = cookies

    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    assert A.effective_device_key(_FR({"attend_dk": "too-short"})) == ""  # 格式不合法
    assert A.effective_device_key(_FR({"attend_dk": "unboundname00000000000000"})) == ""  # 未綁

    bound = "boundname0000000000000003"
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('CD','脈絡設計')")
        cid = con.execute("SELECT id FROM courses").fetchone()["id"]
        con.execute("INSERT INTO students(course_id,student_id,name,device_hash) VALUES(?,?,?,?)",
                    (cid, "9994A009", "學生9", A.hash_device(bound)))
        req = _FR({"attend_dk": bound})
        assert A.effective_device_key(req, con) == bound  # con 有給就沿用
    assert A.effective_device_key(_FR({"attend_dk": bound})) == bound  # 沒給就臨時開一條


def test_C_S7_migrate_legacy_cookie_noop_when_new_name_already_present(A, monkeypatch):
    """新名已經讀得到時不必補發，函式原樣放行不動 cookie。"""
    from starlette.responses import Response

    class _FR:
        def __init__(self, cookies):
            self.cookies = cookies

    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    req = _FR({"__Host-attend_dk": "alreadynewname00000000000"})
    out = A._migrate_legacy_cookie(req, Response(), "alreadynewname00000000000")
    assert not any(k == b"set-cookie" for k, v in out.raw_headers)


def test_sessions_kind_voided_migration_preserves_data(tmp_path):
    """B030/B028：正式站有既有資料，起動時只能用 PRAGMA table_info 檢查後 ALTER TABLE 補欄位，
    不能要求重建資料庫。這裡手刻一份沒有 kind／voided_at 的「舊」sessions/courses 表，
    塞一筆既有場次，再讓 app 用這份資料庫開機，驗遷移後：欄位補上了、既有場次視為一般場次
    （kind='normal'、voided_at IS NULL），且原本的欄位值（含 closed_at／teacher_ip）都還在。"""
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE sessions(
      id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL, secret TEXT NOT NULL,
      opened_at REAL NOT NULL, open_until REAL NOT NULL, late_until REAL NOT NULL,
      closed_at REAL, teacher_ip TEXT, note TEXT DEFAULT '')""")
    con.execute("CREATE TABLE courses(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, "
                "settings TEXT NOT NULL DEFAULT '{}')")
    con.execute("INSERT INTO courses(code,name) VALUES('OLD','舊課')")
    con.execute("INSERT INTO sessions(course_id,secret,opened_at,open_until,late_until,closed_at,teacher_ip,note) "
                "VALUES(1,'oldsecret',1000,1100,1200,1200,'1.2.3.4','')")
    con.commit(); con.close()
    os.environ["ATTEND_DB"] = str(db_path)
    import app
    importlib.reload(app)  # 觸發 app.py 模組載入時的 PRAGMA table_info 檢查與 ALTER TABLE 遷移
    with app.db() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
        assert {"kind", "voided_at"} <= cols, f"遷移沒有補上欄位：{cols}"
        row = c.execute("SELECT * FROM sessions WHERE id=1").fetchone()
    assert row is not None, "既有場次在遷移中掉了"
    assert row["kind"] == "normal", f"既有場次應視為一般場次，實得 {row['kind']!r}"
    assert row["voided_at"] is None, f"既有場次不該被視為已作廢，實得 {row['voided_at']!r}"
    assert row["secret"] == "oldsecret" and row["teacher_ip"] == "1.2.3.4" and row["closed_at"] == 1200, (
        "既有欄位的值在遷移中被動到了")


def test_every_template_compiles(A):
    """樣板語法錯（例如 CSS 寫出 `{#qr` 被 Jinja 當成註解開頭）要在單元層一秒內現形，
    不要等到 e2e 逾時 30 秒才知道整頁 500；djlint 抓不到這種錯。"""
    from pathlib import Path
    names = sorted(p.name for p in (Path(A.__file__).parent / "templates").glob("*.html"))
    assert names
    for n in names:
        A.templates.env.get_template(n)


# ───────────────────────── 名冊副檔名不得混入版控／映像 ─────────────────────────

def _service_dir_if_real_checkout():
    """mutmut 的 mutants/ 沙箱只複製原始碼（app.py／manage.py／templates／static／tests），
    不含 .git、.gitignore、.dockerignore；這裡的測試驗的是專案根目錄的檔案，在沙箱裡沒有
    對象可驗，跳過即可（不是放寬檢查，是這條規則在沙箱裡本來就不適用）。"""
    import subprocess
    from pathlib import Path

    service_dir = Path(__file__).resolve().parent.parent
    if not (service_dir / ".dockerignore").exists() or not (service_dir / ".gitignore").exists():
        return None
    in_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=service_dir, capture_output=True, text=True,
    ).returncode == 0
    return service_dir if in_repo else None


def test_gitignore_and_dockerignore_exclude_roster_extensions():
    """公開版舊的快速開始教人把名冊快速放在服務根目錄（例如 roster.csv），data/ 排除不到那裡；
    名冊含真實學生學號姓名，兩個忽略清單都要擋住常見的名冊副檔名。這裡用假檔名驗證規則本身
    （不讀、不建立任何真實名冊內容），不真的跑 docker build。"""
    import fnmatch
    import subprocess
    import pytest

    service_dir = _service_dir_if_real_checkout()
    if service_dir is None:
        pytest.skip("不在真實的 git 檢出目錄（例如 mutmut 沙箱），沒有 .gitignore／.dockerignore 可驗")
    fake_names = ["roster.csv", "roster.xls", "roster.xlsx", "students.csv"]

    # .gitignore：用 git check-ignore 驗（服務目錄本身在 server repo 版控內）
    for name in fake_names:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--", name],
            cwd=service_dir,
        )
        assert r.returncode == 0, f"{name} 沒有被 .gitignore 排除（git check-ignore 沒命中）"

    # .dockerignore：docker 的樣式語法與 .gitignore 相容，這裡不跑 docker build，
    # 改用 fnmatch 對逐行樣式做等價比對（單層檔名，不含目錄的樣式才適用 fnmatch 直接比對）
    patterns = [
        line.strip() for line in (service_dir / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for name in fake_names:
        assert any(fnmatch.fnmatch(name, pat) for pat in patterns), (
            f"{name} 沒有被 .dockerignore 任何一條樣式排除：{patterns}"
        )


def test_no_roster_csv_or_excel_tracked_in_git():
    """先確認版控裡目前沒有任何名冊 csv／xls／xlsx 被追蹤——這條規則是「加排除規則」，
    不是「事後清歷史」，若已有被追蹤的檔案代表情況不一樣，要停下來回報而不是自動處理。"""
    import subprocess
    import pytest

    service_dir = _service_dir_if_real_checkout()
    if service_dir is None:
        pytest.skip("不在真實的 git 檢出目錄（例如 mutmut 沙箱），無法查 git ls-files")
    r = subprocess.run(
        ["git", "ls-files", "."],
        cwd=service_dir, capture_output=True, text=True, check=True,
    )
    tracked = [line for line in r.stdout.splitlines() if line.lower().endswith((".csv", ".xls", ".xlsx"))]
    assert tracked == [], f"已有名冊類檔案被 git 追蹤，需要人工判斷而非自動排除：{tracked}"
