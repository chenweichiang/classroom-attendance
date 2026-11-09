"""API 與頁面全覆蓋：每個端點在每個 phase 的行為、錯誤路徑、對帳。"""
import os
import stat
import time, csv, io, threading

import pytest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_login_flow(client):
    assert client.get("/t", follow_redirects=False).status_code == 303
    assert client.get("/api/t/session/1/state").status_code == 401
    r = client.post("/t/login", data={"password": "中文亂打", "next": "/t"}); assert r.status_code == 401 and "密碼錯誤" in r.text
    r = client.post("/t/login", data={"password": "pw", "next": "//evil.com"}, follow_redirects=False); assert r.headers["location"] == "/t"
    r = client.post("/t/login", data={"password": "pw", "next": "/t/course/1/students"}, follow_redirects=False); assert r.headers["location"] == "/t/course/1/students"
    assert client.get("/t", follow_redirects=False).status_code == 200
    assert client.post("/t/logout", follow_redirects=False).status_code == 303
    assert client.get("/t", follow_redirects=False).status_code == 303


# 老師 cookie 失效時按 POST 操作，登入後不要落到 405

def _parse_login_redirect(location):
    from urllib.parse import parse_qs, unquote, urlparse
    loc = urlparse(location)
    qs = parse_qs(loc.query)
    next_ = unquote(qs["next"][0]) if "next" in qs else None
    redo = qs.get("redo", [None])[0]
    return loc.path, next_, redo


def test_post_unauthenticated_same_host_referer_redirects_with_redo(h):
    cid = h.cid["CD"]
    anon = h.student()
    r = anon.post(f"/t/course/{cid}/open", data={"kind": "normal"},
                  headers={"referer": f"http://test/t/course/{cid}"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    path, next_, redo = _parse_login_redirect(r.headers["location"])
    assert path == "/t/login"
    assert next_ == f"/t/course/{cid}", (next_, r.headers["location"])
    assert redo == "1"
    with h.A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM sessions WHERE course_id=?", (cid,)).fetchone()[0]
    assert n == 0, "沒有登入不該真的開出場次"


def test_post_unauthenticated_cross_host_or_missing_referer_falls_back_to_t(h):
    cid = h.cid["CD"]
    anon = h.student()
    r1 = anon.post(f"/t/course/{cid}/open", data={"kind": "normal"},
                   headers={"referer": "http://evil.example/t/course/1"}, follow_redirects=False)
    _, next1, _ = _parse_login_redirect(r1.headers["location"])
    assert next1 == "/t", r1.headers["location"]
    r2 = anon.post(f"/t/course/{cid}/open", data={"kind": "normal"}, follow_redirects=False)
    _, next2, _ = _parse_login_redirect(r2.headers["location"])
    assert next2 == "/t", r2.headers["location"]


def test_post_unauthenticated_same_host_referer_outside_t_falls_back_to_t(h):
    """同主機的 Referer 也要限定老師頁：路徑不是 /t 開頭（學生頁 /c）或正好是 /t/login 時，
    next 一律退回 /t（只要路徑條件被拿掉，前面兩支同類測試照樣全綠，所以要單獨釘這條）。"""
    cid = h.cid["CD"]
    anon = h.student()
    for ref in ("http://test/c", "http://test/t/login?next=/t/course/1"):
        r = anon.post(f"/t/course/{cid}/open", data={"kind": "normal"},
                      headers={"referer": ref}, follow_redirects=False)
        _, next_, redo = _parse_login_redirect(r.headers["location"])
        assert next_ == "/t", (ref, r.headers["location"])
        assert redo == "1"


def test_get_unauthenticated_uses_request_path_without_redo(h):
    anon = h.student()
    r = anon.get("/t/session/1", follow_redirects=False)
    assert r.status_code == 303
    path, next_, redo = _parse_login_redirect(r.headers["location"])
    assert next_ == "/t/session/1"
    assert redo is None, "GET 不該帶 redo"


def test_login_page_shows_redo_message(client):
    r = client.get("/t/login", params={"next": "/t", "redo": "1"})
    assert "剛才按的操作沒有送出" in r.text


def test_login_expired_fail_records_are_pruned_on_next_attempt(A, client, monkeypatch):
    """上鎖後，`login()` 裡清過期鍵的那行（`_login_fail.pop(k, None)`）要真的被跑到：
    某個 IP 的登入失敗紀錄過了 600 秒，換一個不同 IP 再次登入失敗時，第一個 IP 的舊紀錄要被
    清掉——否則這個字典會無限成長，鎖只是保護了一段永遠沒被走到的程式碼。"""
    t0 = 1_700_000_000.0
    monkeypatch.setattr(A, "now", lambda: t0)
    client.post("/t/login", data={"password": "x"}, headers={"cf-connecting-ip": "9.9.9.9"})
    assert "9.9.9.9" in A._login_fail, "第一次登入失敗應該先留下紀錄"
    monkeypatch.setattr(A, "now", lambda: t0 + 601)
    client.post("/t/login", data={"password": "x"}, headers={"cf-connecting-ip": "1.1.1.1"})
    assert "9.9.9.9" not in A._login_fail, "過期 600 秒的失敗紀錄應該被清掉，不是留著等它自己長大"
    assert "1.1.1.1" in A._login_fail


def test_login_rate_limit(client, monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    for _ in range(7):
        assert client.post("/t/login", data={"password": "x"}, headers={"cf-connecting-ip": "203.0.113.5"}).status_code == 401
    assert client.post("/t/login", data={"password": "x"}, headers={"cf-connecting-ip": "203.0.113.5"}).status_code == 429  # 第 8 次起
    # 學生在同一出口猜錯密碼，老師用正確密碼照樣登得進去
    assert client.post("/t/login", data={"password": "pw"}, headers={"cf-connecting-ip": "203.0.113.5"}, follow_redirects=False).status_code == 303


def test_logout_revokes_all_devices(A, seeded):
    from fastapi.testclient import TestClient
    a, b = TestClient(A.app, base_url="http://test"), TestClient(A.app, base_url="http://test")
    for c in (a, b):
        c.post("/t/login", data={"password": "pw", "next": "/t"}, follow_redirects=False)
    assert a.get("/t", follow_redirects=False).status_code == 200 and b.get("/t", follow_redirects=False).status_code == 200
    a.post("/t/logout", follow_redirects=False)
    assert b.get("/t", follow_redirects=False).status_code == 303  # 另一台也被登出
    assert b.get("/api/t/session/1/state").status_code == 401


def test_public_pages_and_errors(client):
    for u in ("/help", "/c", "/me", "/t/login", "/healthz", "/api/ip"):
        assert client.get(u).status_code == 200, u
    assert client.get("/", follow_redirects=False).headers["location"] == "/c"
    r = client.get("/nope"); assert r.status_code == 404 and "沒有這個頁面" in r.text
    r = client.get("/s/abc/1/x"); assert r.status_code == 400 and "text/html" in r.headers["content-type"]
    assert client.get("/api/nope").status_code == 404 and client.get("/api/nope").json()["ok"] is False
    assert client.get("/api/ip", headers={"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "<x>"}).text == "203.0.113.9"
    assert client.get("/static/fonts/ZhuqueFangsong-subset.woff2").headers["content-type"].startswith("font/woff2")
    assert client.get("/help").headers["cache-control"] == "private, no-store"
    assert "max-age" in client.get("/static/fonts/ZhuqueFangsong-subset.woff2").headers["cache-control"]
    fr = client.get("/favicon.ico"); assert fr.status_code == 204 and "max-age" in fr.headers["cache-control"]
    # 字型網址要帶內容雜湊，重做子集後 Cloudflare 邊緣與瀏覽器的七天快取才會自動失效
    import hashlib, pathlib
    font = pathlib.Path(__file__).resolve().parent.parent / "static" / "fonts" / "ZhuqueFangsong-subset.woff2"
    v = hashlib.sha256(font.read_bytes()).hexdigest()[:10]
    assert f"ZhuqueFangsong-subset.woff2?v={v})" in client.get("/help").text
    assert client.get(f"/static/fonts/ZhuqueFangsong-subset.woff2?v={v}").status_code == 200


# /healthz 要偵測「連得到但寫不進去」
# 部署時 chown 沒加 -R，資料庫檔或所在目錄可能不是容器帳號可寫；舊版 healthz 只跑 SELECT 1，
# 這種情況照樣回 ok、Docker 顯示 healthy，但一按「開始點名」就 500（readonly database）。

_ROOT_SKIP_REASON = "以 root 身分跑時 os.access 永遠回可寫，這條測試在 root 下驗不出東西"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason=_ROOT_SKIP_REASON)
def test_healthz_503_when_db_file_not_writable(A, client):
    db_path = A.DB_PATH
    with A.db() as con:
        con.execute("SELECT 1")  # 確保檔案已經存在
    old_mode = os.stat(db_path).st_mode
    os.chmod(db_path, stat.S_IRUSR)
    try:
        r = client.get("/healthz")
        assert r.status_code == 503, r.text
        assert "writable" in r.text
    finally:
        os.chmod(db_path, old_mode)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason=_ROOT_SKIP_REASON)
def test_healthz_503_when_db_dir_not_writable(A, client):
    db_dir = os.path.dirname(A.DB_PATH)
    old_mode = os.stat(db_dir).st_mode
    os.chmod(db_dir, stat.S_IRUSR | stat.S_IXUSR)  # 唯讀＋可進入，模擬目錄不可寫但檔案本身權限沒問題
    try:
        r = client.get("/healthz")
        assert r.status_code == 503, r.text
        assert "writable" in r.text
    finally:
        os.chmod(db_dir, old_mode)


def test_healthz_ok_when_writable(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_code_page_paths(h):
    r = h.student().post("/c", data={"code": "123456"}); assert r.status_code == 400 and "沒有進行中" in r.text
    sid = h.open("CD")
    r = h.student().post("/c", data={"code": "000000"}); assert r.status_code == 400 and "代碼不對" in r.text
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    r = h.student().post("/c", data={"code": code[:3] + " " + code[3:]}); assert r.status_code == 200 and 'GRANT="' in r.text
    fw = code.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    assert h.student().post("/c", data={"code": fw}).status_code == 200  # 全形數字也收
    for bad in ("²³¹²³¹", "٣٣٣٣٣٣", "\x0b\x13ab", "１２３"):
        assert h.student().post("/c", data={"code": bad}).status_code in (400, 429), bad  # 絕不 500
    # 同一個校園出口 IP 上，有裝置 cookie 的甲瀏覽器輸錯 10 次被擋（per-device 限流不受影響）
    dev = h.student(); dev.get("/c")  # 先拿伺服器下發的裝置 cookie
    for _ in range(10):
        dev.post("/c", data={"code": "000000"}, headers={"cf-connecting-ip": "198.51.100.77", "user-agent": "A"})
    assert dev.post("/c", data={"code": "000000"}, headers={"cf-connecting-ip": "198.51.100.77", "user-agent": "A"}).status_code == 429
    # 沒有 cookie（退回指紋）的一批人共用同一個放寬到 IP 同級的額度，10 次內不會被擋
    for _ in range(10):
        assert h.student().post("/c", data={"code": "000000"}, headers={"cf-connecting-ip": "198.51.100.77", "user-agent": "B"}).status_code == 400
    assert h.student().post("/c", data={"code": code}, headers={"cf-connecting-ip": "198.51.100.77", "user-agent": "B"}).status_code == 200


def test_student_entry_url(h):
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10); sig = h.A.slot_sig(s, slot)
    assert h.student().get(f"/s/{sid}/{slot}/{sig}").status_code == 200
    assert h.student().get(f"/s/{sid}/{slot}/中文").status_code == 410
    assert h.student().get(f"/s/{sid}/{slot - 5}/{sig}").status_code == 410
    assert h.student().get(f"/s/{sid}/{slot}/{'0' * 10}").status_code == 410
    assert h.student().get(f"/s/999/{slot}/{sig}").status_code == 404
    h.T.post(f"/api/t/session/{sid}/close")
    slot = int(h.A.now() // 10)
    r = h.student().get(f"/s/{sid}/{slot}/{h.A.slot_sig(s, slot)}"); assert r.status_code == 200 and "點名已結束" in r.text


def test_checkin_validation(h):
    sid = h.open("CD")
    g = h.grant(sid, h.dev("A"))
    def post(dev="A", **kw):  # 一個 dev 名稱＝一支手機（各自的 cookie）
        d = {"grant": g, "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234"}; d.update(kw)
        return h.dev(dev).post("/api/checkin", data=d)
    assert post(grant="g:1:x:0:bad").status_code == 410
    # 五段憑證簽章正確，但第四段（issued）不是數字——防禦性 ValueError 分支，一樣視為過期
    dh16_a = h.A.hash_device(h.dev("A").cookies.get("attend_dk"))[:16]
    bad_issued = h.A.make_token("g", str(sid), "n", "not-a-float", dh16_a, exp=h.A.now() + 60)
    r_bad_issued = post(grant=bad_issued)
    assert r_bad_issued.status_code == 410 and "填表時間" in r_bad_issued.json()["error"]
    r = h.student().post("/api/checkin", data={"grant": g, "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234"})
    assert r.status_code == 400 and "識別" in r.json()["error"]  # 沒有伺服器下發的 cookie（無痕）
    assert post(pin="12").status_code == 400 and post(pin="abcd").status_code == 400
    r1 = post(student_id="9999X999"); r2 = post(name="王小明")
    assert r1.status_code == 400 and r2.status_code == 400 and r1.json()["error"] == r2.json()["error"]  # 不在名冊與姓名不符不可區分
    assert post(pin2="9999").status_code == 400
    j = post().json(); assert j["ok"] and j["first_time"] and j["status"] == "出席"
    # 換手機，用對 PIN：409；用錯 PIN：401（PIN 沒過不透露綁定狀態）
    assert post(dev="B", grant=h.grant(sid, h.dev("B"))).status_code == 409
    assert post(dev="B", grant=h.grant(sid, h.dev("B")), pin="9999", pin2="9999").status_code == 401
    # 同一 grant 換手機
    assert post(dev="C").status_code == 410
    # PIN 錯三次鎖（A2：已綁裝置回簽免 PIN，這段改用一支從未綁定過的裝置「Z」才吃得到 PIN 驗證；
    # 只要請求帶了 pin2，一律回「已經設過 PIN」，不看剩幾次；失敗計次照舊要計，
    # 故第 4 次不管 PIN 對不對都已經鎖了——鎖定本身在下一行用正確 PIN 也拿 423 獨立驗證。
    # 每次都換一組不同的錯 PIN——同一個錯 PIN 60 秒內重送不再重複計次，這裡要驗證的是
    # 「三次不同的錯猜」真的鎖起來，不是靠重送同一個值湊出三筆）
    g2 = h.grant(sid, h.dev("Z"))
    for wrong_pin in ("9991", "9992", "9993"):
        r = post(dev="Z", grant=g2, pin=wrong_pin, pin2=wrong_pin)
        assert r.status_code == 401 and "已經設過 PIN" in r.json()["error"]
    assert post(dev="Z", grant=g2).status_code == 423  # 鎖定中（這次帶的是正確 PIN，仍被早鎖擋下）
    # grant 過期／未過期
    assert post(grant=h.A.make_token("g", str(sid), "n", exp=h.A.now() - 1)).status_code == 410
    assert "填表時間" in post(grant=h.A.make_token("g", str(sid), "n", exp=h.A.now() - 1)).json()["error"]
    # 已結束：未鎖定的學生恆 410
    h.T.post(f"/api/t/session/{sid}/close")
    # grant 多帶一段發證時間（g, sid, nonce, issued），再多帶一段送出裝置的雜湊前 16 碼，
    # 四段式（無裝置雜湊）與三段式舊格式一律視為無效不相容
    d_dev = h.dev("D")
    dh16 = h.A.hash_device(d_dev.cookies.get("attend_dk"))[:16]
    r = post(dev="D", grant=h.A.make_token("g", str(sid), "z", str(h.A.now()), dh16, exp=h.A.now() + 60),
             student_id="9994A002", name="學生2")
    # 沒有新場次時的訊息是「點名已經結束。有到課但沒簽到的同學，請直接找老師。」
    assert r.status_code == 410 and "點名已經結束" in r.json()["error"]


def test_already_manual_row_gets_conflict_flags_but_keeps_status(h):
    """老師手動設過狀態後，借用他人已綁定裝置以該學號重新「首次綁定」——已經有紀錄那一列
    要補上 device_bound_other／same_device 讓老師看到，但狀態不能被改掉（見 do_checkin 的已有紀錄分支）。"""
    sid = h.open("CD")
    h.checkin(sid, "devF", "9994A001", name="學生1", pin="1111")  # devF 綁定 A，之後也算 same 的來源
    b_pk = h.pk("CD", "9994A002")
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": b_pk, "status": "excused"})
    r = h.dev("devF").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devF")), "student_id": "9994A002", "name": "學生2", "pin": "9999", "pin2": "9999"})
    j = r.json()
    assert j["ok"] and j["again"] and j["status"] == "請假"  # 狀態維持老師設的「請假」
    row = h.row(sid, "9994A002")
    assert row["status"] == "excused"
    assert "device_bound_other" in row["flags"] and "same_device" in row["flags"]


def test_wrong_pin_without_pin2_counts_down_then_locks(h):
    """對照 test_checkin_validation 裡帶 pin2 的分支：沒帶 pin2（真實換手機情境不會送這欄）時，
    PIN 錯誤照舊走「剩 N 次」倒數、最後鎖定，不是帶 pin2 時那句解釋訊息（do_checkin 的 PIN 驗證分支）。
    已綁裝置回簽免 PIN，這裡改用另一支從未綁定過的裝置「E2」才吃得到 PIN 驗證。
    每次換一組不同的錯 PIN——同一個錯 PIN 60 秒內重送不再重複計次，這裡要驗證的是
    「三次不同的錯猜」真的倒數到鎖定，不是靠重送同一個值湊出三筆。"""
    sid = h.open("CD")
    g = h.grant(sid, h.dev("E"))
    r = h.dev("E").post("/api/checkin", data={"grant": g, "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234"})
    assert r.json()["ok"]
    for left, wrong_pin in ((2, "0001"), (1, "0002")):
        r = h.dev("E2").post("/api/checkin", data={"grant": h.grant(sid, h.dev("E2")), "student_id": "9994A001", "pin": wrong_pin})
        assert r.status_code == 401 and f"剩 {left} 次" in r.json()["error"]
    r = h.dev("E2").post("/api/checkin", data={"grant": h.grant(sid, h.dev("E2")), "student_id": "9994A001", "pin": "0003"})
    assert r.status_code == 401 and "手動" in r.json()["error"]
    r = h.dev("E2").post("/api/checkin", data={"grant": h.grant(sid, h.dev("E2")), "student_id": "9994A001", "pin": "1234"})
    assert r.status_code == 423  # 鎖定中，正確 PIN 也進不去


def test_returning_device_bad_pin_format_rejected(h):
    """已綁裝置換一支新裝置回簽（不是 same_device_as_bound），PIN 格式不對（不是 4–6 碼數字）
    要在「確定需要 PIN」之後才擋格式，回 pin_format，不計入失敗次數。"""
    sid = h.open("CD")
    h.checkin(sid, "api-fmt-owner", "9994A001", name="學生1", pin="1234")
    r = h.dev("api-fmt-other").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("api-fmt-other")), "student_id": "9994A001", "pin": "12"})
    assert r.status_code == 400 and "4 到 6 位數字" in r.json()["error"]


def test_late_and_pending_and_ip(h):
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET open_until=? WHERE id=?", (h.A.now() - 1, sid))
    j = h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1", ip="1.2.3.4"); assert j["status"] == "遲到"
    h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "140.131.1.1"})
    j = h.checkin(sid, "d2-1234567890123456", "9994A002", "學生2", ip="1.2.3.4"); assert "ip_differs" in h.row(sid, "9994A002")["flags"]
    j = h.checkin(sid, "d2-1234567890123456", "9994A003", "學生3"); assert j["status"] == "待確認"
    assert h.state(sid)["counts"]["pending"] == 2 and h.state(sid)["phase"] == "late"  # 同手機的兩人都待確認


def test_teacher_endpoints(h):
    sid = h.open("CD")
    assert h.T.get(f"/t/session/{sid}").status_code == 200 and h.T.get(f"/t/projector/{sid}").status_code == 200
    q = h.T.get(f"/api/t/session/{sid}/qr").json(); assert q["svg"].startswith("<") and q["total"] == 4 and q["phase"] == "open"
    j = h.T.post(f"/api/t/session/{sid}/spotcheck").json(); assert j["ok"] and j["picks"] == [] and "還沒有人" in j["note"]
    h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1")
    j = h.T.post(f"/api/t/session/{sid}/spotcheck").json(); assert len(j["picks"]) == 1
    assert "都抽過" in h.T.post(f"/api/t/session/{sid}/spotcheck").json()["note"]
    assert h.T.post(f"/api/t/spotcheck/{j['picks'][0]['id']}", data={"present": "1"}).json()["ok"]
    assert "spot_ok" in h.row(sid, "9994A001")["flags"]
    assert h.T.post("/api/t/spotcheck/9999", data={"present": "1"}).status_code == 404
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": 1, "status": "bogus"}).status_code == 400
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": 9999, "status": "present"}).status_code == 404
    pk4 = h.pk("CD", "9994A004")
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk4, "status": "excused"}).json()["ok"]
    assert h.row(sid, "9994A004")["status"] == "excused" and h.row(sid, "9994A004")["ts"] == ""
    assert h.T.post(f"/api/t/session/{sid}/notify").status_code == 409
    assert h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 1}).json()["ok"]
    assert h.T.post("/api/t/student/9999/unbind", data={"reset_pin": ""}).status_code == 404
    assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
    assert h.T.post(f"/api/t/session/{sid}/spotcheck").status_code == 409
    assert h.T.post(f"/api/t/spotcheck/{j['picks'][0]['id']}", data={"present": "0"}).status_code == 409
    assert h.T.get(f"/api/t/session/{sid}/qr").json()["svg"] == ""
    assert h.T.post(f"/api/t/session/{sid}/notify").status_code == 424  # 沒 token 回 424：Cloudflare 對 origin 5xx 會換成自己的錯誤頁
    assert h.T.get("/t").status_code == 200 and h.T.get(f"/t/course/{h.cid['CD']}/report").status_code == 200
    assert h.T.get(f"/t/course/{h.cid['CD']}/students").status_code == 200
    assert h.T.get("/t/session/9999").status_code == 404 and h.T.get("/t/course/9999/report").status_code == 404
    assert h.T.post(f"/api/t/session/{sid}/delete").json()["ok"] and h.T.get(f"/t/session/{sid}").status_code == 404


def test_settings_and_bind_toggle(h):
    cid = h.cid["CD"]
    assert h.T.post(f"/t/course/{cid}/settings", data={"open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 5, "spot_n": 3, "ip_check": "", "discord_channel": "abc123def"}).status_code == 400  # 非法頻道 ID 退回，不靜默改寫
    r = h.T.post(f"/t/course/{cid}/settings", data={"open_minutes": 0, "late_minutes": 1, "qr_interval": 2, "qr_grace": 99, "spot_n": 0, "ip_check": "", "discord_channel": "12345678901234567"}, follow_redirects=False)
    assert r.status_code == 303
    with h.A.db() as con:
        st = h.A.course_settings(h.A.get_course(con, cid))
    assert st == {**st, "open_minutes": 1, "late_minutes": 1, "qr_interval": 5, "qr_grace": 99, "spot_n": 1, "ip_check": False, "discord_channel": "12345678901234567", "allow_bind": True}  # qr_grace 上限 120，不再夾在 qr_interval 以內
    # 開放 0、遲到 0 也不會開出一開始就結束的場次
    h.T.post(f"/t/course/{cid}/settings", data={"open_minutes": 0, "late_minutes": 0, "qr_interval": 10, "qr_grace": 5, "spot_n": 3, "ip_check": "1", "discord_channel": ""})
    sid0 = h.open("CD"); q = h.T.get(f"/api/t/session/{sid0}/qr").json(); assert q["phase"] == "open" and q["svg"]
    h.T.post(f"/api/t/session/{sid0}/delete")
    assert h.T.post(f"/api/t/course/{cid}/bind", data={"allow": "0"}).json()["allow_bind"] is False
    sid = h.open("CD")
    assert "不開放首次綁定" in h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1")["error"]
    assert h.T.post("/api/t/course/9999/bind", data={"allow": "1"}).status_code == 404


def test_discord_summary_and_close(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?", ('{"discord_channel": "42"}', h.cid["CD"]))
    sid = h.open("CD")
    # 手動結束也套用「出席不到一半就暫緩發送」，這裡兩人簽到（2/4=0.5，不小於門檻）
    # 才會照發；只簽一人（1/4）會被暫緩，那個情境另外在 test_teacher_friction.py 測過。
    h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1")
    h.checkin(sid, "d2-1234567890123456", "9994A002", "學生2")
    assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True
    for _ in range(50):
        if sent: break
        time.sleep(0.05)
    assert sent and sent[0][0] == "42" and "缺席 2" in sent[0][1] and "9994A003 學生3" in sent[0][1]
    assert h.T.post(f"/api/t/session/{sid}/notify").json()["ok"] and len(sent) == 2


def test_report_me_csv_consistency(h):
    sid = h.open("CD")
    h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1")
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A002"), "status": "excused"})
    h.T.post(f"/api/t/session/{sid}/close")
    # 加選：A005 在第一場之後才進名冊
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,created_at) VALUES(?,?,?,?)", (h.cid["CD"], "9994A005", "學生5", h.A.now()))
    sid2 = h.open("CD"); h.checkin(sid2, "d5-1234567890123456", "9994A005", "學生5"); h.T.post(f"/api/t/session/{sid2}/close")
    with h.A.db() as con:
        sessions, rows = h.A.build_report(con, h.cid["CD"])
    rep = {r["stu"]["student_id"]: r for r in rows}
    assert rep["9994A005"]["cells"] == ["na", "present"] and rep["9994A005"]["rate"] == 1.0
    assert rep["9994A002"]["cells"] == ["excused", "absent"] and rep["9994A002"]["rate"] == 0.0
    assert rep["9994A001"]["rate"] == 0.5
    html = h.T.get(f"/t/course/{h.cid['CD']}/report").text; assert "—" in html and "50%" in html
    rows_csv = list(csv.reader(io.StringIO(h.T.get(f"/t/course/{h.cid['CD']}/export/csv").text.lstrip("﻿"))))
    a5 = next(r for r in rows_csv if r[0] == "9994A005"); assert a5[3] == "—" and a5[-1] == "100%"
    me = h.dev("d5-1234567890123456").post("/api/me").json()["courses"][0]
    assert len(me["items"]) == 1 and me["rate"] == 1.0
    assert h.student().post("/api/me").json()["courses"] == []
    assert h.student().post("/api/whoami", data={"course_id": h.cid["CD"]}).json()["known"] is False
    dev = h.dev("d5-1234567890123456")
    for _ in range(30):
        dev.post("/api/me")
    assert dev.post("/api/me").status_code == 429  # 同裝置查太多次


def test_concurrent_same_student(h):
    sid = h.open("CD"); Z = h.dev("dz"); g = h.grant(sid, Z); res = []
    def go():
        res.append(Z.post("/api/checkin", data={"grant": g, "student_id": "9994A004", "name": "學生4", "pin": "1234", "pin2": "1234"}).json())
    ths = [threading.Thread(target=go) for _ in range(6)]; [t.start() for t in ths]; [t.join() for t in ths]
    assert all(r["ok"] for r in res) and sum(1 for r in res if not r["again"]) == 1


def test_unbind_reset_and_old_pin_hint(h):
    sid = h.open("CD"); h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1")
    pk = h.pk("CD", "9994A001")
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    j = h.checkin(sid, "d9-1234567890123456", "9994A001", "學生1", pin="5555"); assert "原本的 PIN" in j["error"]
    j = h.checkin(sid, "d9-1234567890123456", "9994A001", "學生1", pin="1234"); assert j["ok"] and j["again"]
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"})
    j = h.checkin(sid, "d8-1234567890123456", "9994A001", "學生1", pin="7777"); assert j["ok"] and j["again"]
    with h.A.db() as con:
        assert con.execute("SELECT device_hash IS NOT NULL FROM students WHERE id=?", (pk,)).fetchone()[0] == 1


def test_manage_cli(A, tmp_path, capsys):
    import subprocess, sys, os
    roster = tmp_path / "r.csv"; roster.write_text("student_id,name,class\n9994A001,甲,一甲\n9994A002,乙,一甲\n", encoding="utf-8")
    env = {**os.environ, "ATTEND_DB": os.environ["ATTEND_DB"]}
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = lambda *a: subprocess.run([sys.executable, os.path.join(base, "manage.py"), *a], capture_output=True, text=True, env=env, cwd=base)
    assert "新增 2" in run("import-roster", "--code", "X", "--name", "課", "--csv", str(roster)).stdout
    roster.write_text("student_id,name,class\n9994A001,甲改,一甲\n", encoding="utf-8")
    out = run("import-roster", "--code", "X", "--name", "課", "--csv", str(roster), "--prune").stdout; assert "更新 1" in out and "刪除 1" in out
    assert "X.spot_n = 2" in run("set", "--code", "X", "--key", "spot_n", "--value", "2").stdout
    assert run("set", "--code", "X", "--key", "late_minutes", "--value", "1").returncode != 0
    assert "名冊 1 人" in run("list").stdout
    assert "已刪課程" in run("delete-course", "--code", "X").stdout
    assert run("delete-course", "--code", "X").returncode != 0


# manage.py set-setting
# 設定值寫入走 `manage.py set --code <代碼> --key <鍵> --value <值>`（見上方 test_manage_cli）；
# 這裡補三種情境：合法值寫入、非法值被 course_settings 的驗證拉回、不存在的課程代碼報錯。

def _manage_run(base, env):
    import subprocess, sys, os
    return lambda *a: subprocess.run([sys.executable, os.path.join(base, "manage.py"), *a],
                                      capture_output=True, text=True, env=env, cwd=base)


def test_manage_set_writes_legal_value(A):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_run(base, {**os.environ, "ATTEND_DB": os.environ["ATTEND_DB"]})
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('SET1','A1 合法值')")
    r = run("set", "--code", "SET1", "--key", "spot_n", "--value", "7")
    assert r.returncode == 0, r.stderr
    assert "SET1.spot_n = 7" in r.stdout
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='SET1'").fetchone()[0]
        st = A.course_settings(A.get_course(con, cid))
    assert st["spot_n"] == 7


def test_manage_set_out_of_range_is_refused_and_db_untouched(A):
    """manage.py 寫入設定前先用 course_settings 讀回：若 spot_n=0 這類值會被夾成別的值，就非零結束、不寫入、說明實際生效值。
    原樣寫進 DB 的話，指令會印出「= 0」，讀出來卻被 course_settings() 拉回 1，印的就不是實際生效值。"""
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_run(base, {**os.environ, "ATTEND_DB": os.environ["ATTEND_DB"]})
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('SET2','A1 拉回')")
        before = con.execute("SELECT settings FROM courses WHERE code='SET2'").fetchone()[0]
    r = run("set", "--code", "SET2", "--key", "spot_n", "--value", "0")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "實際生效 1" in (r.stdout + r.stderr), r.stdout + r.stderr
    with A.db() as con:
        assert con.execute("SELECT settings FROM courses WHERE code='SET2'").fetchone()[0] == before, "被拒時 DB 一個字都不動"


def test_manage_set_missing_course_code_errors(A):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_run(base, {**os.environ, "ATTEND_DB": os.environ["ATTEND_DB"]})
    r = run("set", "--code", "NOPE-NOT-A-COURSE", "--key", "spot_n", "--value", "3")
    assert r.returncode != 0
    assert "沒有這個課程代碼" in (r.stdout + r.stderr)


def test_teacher_pages_redirect_when_logged_out(client, seeded):
    for u in ("/t/session/1", "/t/projector/1", f"/t/course/{seeded['CD']}/students", f"/t/course/{seeded['CD']}/report", "/t"):
        r = client.get(u, follow_redirects=False); assert r.status_code == 303 and r.headers["location"].startswith("/t/login?next="), u
    assert client.get(f"/t/course/{seeded['CD']}/export/csv", follow_redirects=False).status_code == 303


def test_proxy_partner_absent_but_not_confirmed(h):
    sid = h.open("CD")
    h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1")
    h.checkin(sid, "dA-1234567890123456", "9994A002", "學生2")  # 同手機代簽 → 兩人都 pending
    assert h.db_rows(sid)["9994A001"]["status"] == "pending"
    h.checkin(sid, "dB-1234567890123456", "9994A003", "學生3")
    picks = {}
    for _ in range(3):
        for p in h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"]:
            picks[p["student_id"]] = p["id"]
    assert set(picks) == {"9994A001", "9994A002", "9994A003"}
    h.T.post(f"/api/t/spotcheck/{picks['9994A001']}", data={"present": "1"})   # 老師先確認 A001 在
    h.T.post(f"/api/t/spotcheck/{picks['9994A002']}", data={"present": "0"})   # A002 不在
    d = h.db_rows(sid)
    assert d["9994A002"]["status"] == "absent" and d["9994A001"]["status"] == "present" and "proxy_for" not in d["9994A001"]["flags"]
    # 換個情境：A003 的手機再幫 A004 簽，A004 不在 → A003 連坐（未被確認過）
    h.checkin(sid, "dB-1234567890123456", "9994A004", "學生4")
    sc = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    h.T.post(f"/api/t/spotcheck/{sc['id']}", data={"present": "0"})
    d = h.db_rows(sid); assert d["9994A004"]["status"] == "absent" and d["9994A003"]["status"] == "absent" and "proxy_for" in d["9994A003"]["flags"]
    # 老師改回出席 → 清掉抽點旗標
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A003"), "status": "present"})
    assert "proxy_for" not in h.db_rows(sid)["9994A003"]["flags"] and "manual" in h.db_rows(sid)["9994A003"]["flags"]
    # pending 被判在 → 依 open_until 給出席
    h.checkin(sid, "dA-1234567890123456", "9994A004", "學生4")  # already absent(manual? no: spot) → again
    assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
    assert h.T.post(f"/api/t/session/{sid}/close").json()["note"]
    assert h.T.post("/api/t/session/9999/delete").status_code == 404 and h.T.post("/api/t/session/9999/close").status_code == 404


def test_same_fp_both_and_home_live(h):
    sid = h.open("CD")
    h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1", ua="iPhone/1", hints="x")
    h.checkin(sid, "dB-1234567890123456", "9994A002", "學生2", ua="iPhone/1", hints="zzz")  # hints 不同也照抓：指紋只看伺服器可見訊號
    import json
    d = h.db_rows(sid)
    f1, f2 = json.loads(d["9994A001"]["flags"]), json.loads(d["9994A002"]["flags"])
    assert f1.count("same_fp") == 1 and f2.count("same_fp") == 1, (f1, f2)  # 兩邊都標、各只標一次
    # 同指紋替第二個學號首綁：一般點名轉待確認（不只標記）；先簽的那位維持出席
    assert d["9994A002"]["status"] == "pending" and d["9994A001"]["status"] == "present"
    j = h.checkin(sid, "dC-1234567890123456", "9994A003", "學生3", ua="iPhone/1")  # 同來源兩分鐘內第三筆首綁：bind_burst 照標，同指紋同樣待確認
    assert j["status"] == "待確認" and "bind_burst" in h.db_rows(sid)["9994A003"]["flags"]
    d = h.db_rows(sid)  # 第三人送出後，前兩列的 same_fp 仍各只有一個
    for stu in ("9994A001", "9994A002"):
        fl = json.loads(d[stu]["flags"])
        assert fl.count("same_fp") == 1, (stu, fl)
    html = h.T.get("/t").text; assert "回到點名頁" in html and "點名中" in html
    assert h.T.post(f"/t/course/{h.cid['CD']}/open", follow_redirects=False).headers["location"].endswith(f"/{sid}")
    # 已刪除的場次：手上還開著表單的學生
    Z = h.dev("dZ"); g = h.grant(sid, Z); h.T.post(f"/api/t/session/{sid}/delete")
    r = Z.post("/api/checkin", data={"grant": g, "student_id": "9994A003", "name": "學生3", "pin": "1234", "pin2": "1234"})
    assert r.status_code == 410 and "取消" in r.json()["error"]


from hypothesis import given, settings as hsettings, strategies as st, HealthCheck


@given(st.integers(0, 600), st.integers(0, 600), st.integers(0, 600), st.integers(0, 600))
@hsettings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_settings_always_valid(h, om, lm, iv, gr):
    cid = h.cid["DT"]
    h.T.post(f"/t/course/{cid}/settings", data={"open_minutes": om, "late_minutes": lm, "qr_interval": iv, "qr_grace": gr, "spot_n": 1, "ip_check": "", "discord_channel": ""})
    with h.A.db() as con:
        s = h.A.course_settings(h.A.get_course(con, cid))
    assert s["late_minutes"] >= s["open_minutes"] >= 1 and s["qr_interval"] >= 5 and 0 <= s["qr_grace"] <= 120  # 新上限語意


def test_pending_judged_present_keeps_late_after_extend(h):
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET open_until=? WHERE id=?", (h.A.now() - 1, sid))  # 進遲到區
    h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1")
    h.checkin(sid, "dA-1234567890123456", "9994A002", "學生2")  # 同手機 → 待確認，簽到當下是遲到
    assert h.row(sid, "9994A002")["status"] == "pending"
    h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 5})   # 延長把 open_until 推大
    sc = next(p for p in h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"] + h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"] if p["student_id"] == "9994A002")
    h.T.post(f"/api/t/spotcheck/{sc['id']}", data={"present": "1"})
    assert h.db_rows(sid)["9994A002"]["status"] == "late"


def test_spot_result_after_close_has_no_side_effect(h):
    sid = h.open("CD"); h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1")
    sc = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.T.post(f"/api/t/spotcheck/{sc['id']}", data={"present": "0"}).status_code == 409
    with h.A.db() as con:
        assert con.execute("SELECT result FROM spotchecks WHERE id=?", (sc["id"],)).fetchone()[0] is None
    assert h.db_rows(sid)["9994A001"]["status"] == "present"


def test_whoami_known_and_ambiguous(h):
    sid = h.open("CD"); h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1")
    A = h.dev("dA-1234567890123456")
    w = A.post("/api/whoami", data={"course_id": h.cid["CD"]}).json()
    assert w == {"known": True, "student_id": "9994A001", "name": "學生1"}
    assert A.post("/api/whoami", data={"course_id": h.cid["DT"]}).json()["known"] is False
    with h.A.db() as con:  # 歷史資料：兩個學號綁同一支
        con.execute("UPDATE students SET device_hash=(SELECT device_hash FROM students WHERE student_id='9994A001' AND course_id=?) WHERE student_id='9994A002' AND course_id=?", (h.cid["CD"], h.cid["CD"]))
    assert A.post("/api/whoami", data={"course_id": h.cid["CD"]}).json()["known"] is False


def test_discord_request_shape(h, monkeypatch):
    import urllib.request, json as _json
    seen = {}
    class FakeResp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url; seen["headers"] = dict(req.headers); seen["body"] = _json.loads(req.data.decode()); return FakeResp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    h.A.DISCORD_TOKEN = "tok"
    ok, msg = h.A.discord_post("42", "hello"); assert ok and msg == "HTTP 204"
    assert seen["url"].endswith("/channels/42/messages") and seen["headers"]["Authorization"] == "Bot tok"
    assert seen["headers"]["User-agent"].startswith("DiscordBot") and seen["body"] == {"content": "hello", "allowed_mentions": {"parse": []}}
    FakeResp.status = 429; assert h.A.discord_post("42", "x")[0] is False


def test_summary_excludes_late_added_and_sums(h):
    sid = h.open("CD"); h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1"); h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,created_at) VALUES(?,?,?,?)", (h.cid["CD"], "9994A009", "後加", h.A.now()))
        ch, txt = h.A.session_summary(con, sid)
    head = txt.splitlines()[0]
    import re
    nums = {k: int(v) for k, v in re.findall(r"(出席|遲到|缺席|請假|待確認) (\d+)", head)}
    total = int(re.search(r"全班 (\d+) 人", head).group(1))
    assert sum(nums.values()) == total == 4 and "後加" not in txt


def test_manage_created_at_branches(A, tmp_path):
    import subprocess, sys, os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = lambda *a: subprocess.run([sys.executable, os.path.join(base, "manage.py"), *a], capture_output=True, text=True, env=dict(os.environ), cwd=base)
    r = tmp_path / "r.csv"; r.write_text("student_id,name,class\nS1,甲,\nS2,乙,\n", encoding="utf-8"); run("import-roster", "--code", "X", "--name", "課", "--csv", str(r))
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='X'").fetchone()["id"]
        t = A.now(); con.execute("INSERT INTO sessions(course_id,secret,opened_at,open_until,late_until,closed_at) VALUES(?,?,?,?,?,?)", (cid, "s", t - 100, t - 90, t - 80, t - 80))
    r.write_text("student_id,name,class\nS1,甲,\nS2,乙,\nS3,丙,\n", encoding="utf-8"); run("import-roster", "--code", "X", "--name", "課", "--csv", str(r))
    with A.db() as con:
        ca = {row["student_id"]: row["created_at"] for row in con.execute("SELECT student_id, created_at FROM students WHERE course_id=?", (cid,))}
        sessions, rows = A.build_report(con, cid)
    assert ca["S1"] == 0 and ca["S2"] == 0 and ca["S3"] > 0
    rep = {x["stu"]["student_id"]: x["cells"] for x in rows}; assert rep["S3"] == ["na"] and rep["S1"] == ["absent"]


# import-roster --prune 會刪掉不該刪的學生
# 一律用 tmp_path 底下自建的假名冊 CSV 與這個模組自己的暫存 DB（A fixture 的 ATTEND_DB），
# 絕對不碰 data/rosters/ 底下的真實名冊。

def _manage_runner(base):
    import subprocess, sys, os
    env = {**os.environ, "ATTEND_DB": os.environ["ATTEND_DB"]}
    return lambda *a: subprocess.run([sys.executable, os.path.join(base, "manage.py"), *a],
                                      capture_output=True, text=True, env=env, cwd=base)


def _manage_seed(run, code, n, tmp_path, name_tag=""):
    """先匯入 n 個學生（S001.. 依序），回傳 CSV 內容字串（給呼叫端接下來自己改）。"""
    rows = "student_id,name,class\n" + "".join(f"S{i:03d},學生{i}{name_tag},一甲\n" for i in range(1, n + 1))
    csv_path = tmp_path / f"seed_{code}.csv"
    csv_path.write_text(rows, encoding="utf-8")
    r = run("import-roster", "--code", code, "--name", "課", "--csv", str(csv_path))
    assert r.returncode == 0, r.stdout + r.stderr
    return rows


def test_missing_student_id_header_aborts_without_writing(A, tmp_path):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_runner(base)
    bad = tmp_path / "bad_header.csv"; bad.write_text("name,class\n甲,一甲\n", encoding="utf-8")
    r = run("import-roster", "--code", "DEV1H", "--name", "課", "--csv", str(bad))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "CSV 表頭找不到學號欄" in (r.stdout + r.stderr)
    with A.db() as con:
        assert con.execute("SELECT 1 FROM courses WHERE code='DEV1H'").fetchone() is None, "表頭錯誤時不該建課程、不該寫任何東西"


def test_blank_name_row_blocks_prune_even_with_force(A, tmp_path):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_runner(base)
    _manage_seed(run, "DEV1N", 2, tmp_path)
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1N'").fetchone()["id"]
        before = {row["student_id"]: dict(row) for row in con.execute("SELECT * FROM students WHERE course_id=?", (cid,))}
    new_csv = tmp_path / "dev2n_blank.csv"
    new_csv.write_text("student_id,name,class\nS001,,一甲\nS002,學生2,一甲\n", encoding="utf-8")  # S001 姓名空白
    r = run("import-roster", "--code", "DEV1N", "--name", "課", "--csv", str(new_csv), "--prune")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "姓名空白：第 2 列" in r.stdout
    assert "先修正 CSV 再 --prune" in (r.stdout + r.stderr) and "不能用 --force" in (r.stdout + r.stderr)
    r2 = run("import-roster", "--code", "DEV1N", "--name", "課", "--csv", str(new_csv), "--prune", "--force")
    assert r2.returncode == 1, "姓名空白這個原因不能用 --force 放行"
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1N'").fetchone()["id"]
        after = {row["student_id"]: dict(row) for row in con.execute("SELECT * FROM students WHERE course_id=?", (cid,))}
    assert before == after, "拒絕時資料庫要完全沒動，連姓名更新都不做"


def test_dry_run_prints_plan_without_writing(A, tmp_path):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_runner(base)
    _manage_seed(run, "DEV1D", 4, tmp_path)
    new_csv = tmp_path / "dev2d_new.csv"
    new_csv.write_text("student_id,name,class\nS001,學生1,一甲\nS002,學生2,一甲\n", encoding="utf-8")  # 少了 S003、S004
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1D'").fetchone()["id"]
        con.execute("INSERT INTO sessions(course_id,secret,opened_at,open_until,late_until,closed_at) VALUES(?,?,?,?,?,?)",
                    (cid, "s", A.now() - 100, A.now() - 90, A.now() - 80, A.now() - 80))
        s3pk = con.execute("SELECT id FROM students WHERE course_id=? AND student_id='S003'", (cid,)).fetchone()["id"]
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, source) VALUES(?,?,?,?,?,?,?)",
                    (con.execute("SELECT id FROM sessions WHERE course_id=?", (cid,)).fetchone()["id"], s3pk, A.now(), "", "present", "[]", "qr"))
        before = {row["student_id"] for row in con.execute("SELECT student_id FROM students WHERE course_id=?", (cid,))}
    r = run("import-roster", "--code", "DEV1D", "--name", "課", "--csv", str(new_csv), "--prune", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "會新增 0、更新 2、刪除 2" in r.stdout, r.stdout
    assert "將刪除 S003（1 筆簽到）" in r.stdout, r.stdout
    assert "將刪除 S004（0 筆簽到）" in r.stdout, r.stdout
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1D'").fetchone()["id"]
        after = {row["student_id"] for row in con.execute("SELECT student_id FROM students WHERE course_id=?", (cid,))}
    assert before == after, "--dry-run 不能真的寫資料庫"


def test_prune_one_of_many_under_threshold_succeeds_without_force(A, tmp_path):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_runner(base)
    _manage_seed(run, "DEV1T1", 31, tmp_path)
    rows = "student_id,name,class\n" + "".join(f"S{i:03d},學生{i},一甲\n" for i in range(1, 31))  # 少了 S031
    new_csv = tmp_path / "dev2t1_new.csv"; new_csv.write_text(rows, encoding="utf-8")
    r = run("import-roster", "--code", "DEV1T1", "--name", "課", "--csv", str(new_csv), "--prune")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "刪除 1" in r.stdout
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1T1'").fetchone()["id"]
        n = con.execute("SELECT COUNT(*) FROM students WHERE course_id=?", (cid,)).fetchone()[0]
    assert n == 30


def test_prune_five_of_31_blocked_without_force_then_succeeds_with_force(A, tmp_path):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_runner(base)
    _manage_seed(run, "DEV1T2", 31, tmp_path)
    rows = "student_id,name,class\n" + "".join(f"S{i:03d},學生{i},一甲\n" for i in range(1, 27))  # 少了 S027..S031，共 5 人
    new_csv = tmp_path / "dev2t2_new.csv"; new_csv.write_text(rows, encoding="utf-8")
    r = run("import-roster", "--code", "DEV1T2", "--name", "課", "--csv", str(new_csv), "--prune")
    assert r.returncode == 1, r.stdout + r.stderr
    combined = r.stdout + r.stderr
    assert "超過" in combined and "一成" in combined and "--force" in combined
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1T2'").fetchone()["id"]
        n_before = con.execute("SELECT COUNT(*) FROM students WHERE course_id=?", (cid,)).fetchone()[0]
    assert n_before == 31, "拒絕時不能動資料庫"
    r2 = run("import-roster", "--code", "DEV1T2", "--name", "課", "--csv", str(new_csv), "--prune", "--force")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "刪除 5" in r2.stdout
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1T2'").fetchone()["id"]
        n_after = con.execute("SELECT COUNT(*) FROM students WHERE course_id=?", (cid,)).fetchone()[0]
    assert n_after == 26


def test_non_prune_repeat_import_leaves_pin_binding_and_created_at_untouched(A, tmp_path):
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_runner(base)
    _manage_seed(run, "DEV1U", 2, tmp_path)
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='DEV1U'").fetchone()["id"]
        pk = con.execute("SELECT id FROM students WHERE course_id=? AND student_id='S001'", (cid,)).fetchone()["id"]
        con.execute("UPDATE students SET device_hash='deadbeef', bound_at=123, pin_hash='x', pin_salt='y', created_at=456 WHERE id=?", (pk,))
    new_csv = tmp_path / "dev2u_new.csv"
    new_csv.write_text("student_id,name,class\nS001,改名學生1,一甲\nS002,學生2,一甲\n", encoding="utf-8")
    # 不帶 --prune。已綁定或已設 PIN 的人改名要 --force（沒帶會整份擋下，見 test_manage.py），
    # 這條驗的是「重匯入不動 PIN、綁定與 created_at」，所以帶 --force 走正式改名
    r = run("import-roster", "--code", "DEV1U", "--name", "課", "--csv", str(new_csv), "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    with A.db() as con:
        row = dict(con.execute("SELECT * FROM students WHERE id=?", (pk,)).fetchone())
    assert row["name"] == "改名學生1", "沒帶 --prune 一樣要更新姓名"
    assert row["device_hash"] == "deadbeef" and row["bound_at"] == 123
    assert row["pin_hash"] == "x" and row["pin_salt"] == "y"
    assert row["created_at"] == 456


def test_cookie_flags_and_expired_token(A, seeded):
    import os, importlib
    from fastapi.testclient import TestClient
    os.environ["ATTEND_BASE_URL"] = "https://test"; importlib.reload(A)
    try:
        c = TestClient(A.app, base_url="https://test")
        r = c.post("/t/login", data={"password": "pw", "next": "/t"}, follow_redirects=False)
        sc = r.headers["set-cookie"].lower(); assert "secure" in sc and "httponly" in sc and "samesite=lax" in sc
        c.cookies.set("ta", A.make_token("teacher", exp=A.now() - 1))
        assert c.get("/api/t/session/1/state").status_code == 401 and c.get("/t", follow_redirects=False).status_code == 303
    finally:
        os.environ["ATTEND_BASE_URL"] = "http://test"; importlib.reload(A)


def test_checkin_throttle_and_pin_lock_per_device(h):
    sid = h.open("CD"); S = h.dev("enum"); g = h.grant(sid, S)
    codes = []
    for i in range(13):
        codes.append(S.post("/api/checkin", data={"grant": g, "student_id": f"X{i:03d}", "name": "x", "pin": "1234", "pin2": "1234"}).status_code)
    assert codes[-1] == 429 and 429 not in codes[:12]
    # A001 已設 PIN 但未綁（老師解綁）：別人的手機猜錯 3 次鎖的是那支手機，本人手機仍可簽
    h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1"); pk = h.pk("CD", "9994A001"); h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    # 每次換一組不同的錯 PIN——同一個錯 PIN 60 秒內重送不再重複計次，這裡要驗證的是
    # 「三次不同的錯猜」真的各自計次，不是靠重送同一個值湊出三筆
    for wrong_pin in ("0001", "0002", "0003"):
        h.checkin(sid, "evil-1234567890123456", "9994A001", pin=wrong_pin)
    with h.A.db() as con:
        rows = {r["device_hash"]: r["n"] for r in con.execute("SELECT device_hash, n FROM pin_fail WHERE session_id=? AND student_pk=?", (sid, pk))}
    evil_dh = h.A.hash_device(h.dev("evil-1234567890123456").cookies.get("attend_dk"))
    assert rows[evil_dh] == 3 and rows[""] == 3  # 每裝置與本場總計都有計次（帶 pin2 也算）
    assert h.checkin(sid, "evil-1234567890123456", "9994A001", pin="1234")["ok"] is False  # 那支手機鎖了
    j = h.checkin(sid, "dA-1234567890123456", "9994A001", pin="1234"); assert j["ok"], j
    # 換裝置識別重試：本場總計 20 次後任何裝置都鎖
    sid2 = h.open("DT"); h.checkin(sid2, "dX-1234567890123456", "1153A009", "學生9"); h.T.post(f"/api/t/student/{h.pk('DT', '1153A009')}/unbind", data={"reset_pin": ""})
    for i in range(20):
        h.checkin(sid2, f"rot{i:02d}-1234567890123456", "1153A009", pin="0000", ip=f"10.0.{i}.1")
    assert h.checkin(sid2, "fresh-1234567890123456", "1153A009", pin="1234").get("ok") is not True


def test_many_students_concurrently(h):
    sid = h.open("CD"); res = []
    ids = ["9994A001", "9994A002", "9994A003", "9994A004"]
    devs = [h.dev(f"dev{i}") for i in range(len(ids))]; grants = [h.grant(sid, d) for d in devs]
    def go(i):
        # 每支模擬手機各自的 UA（D3：同指紋會被轉待確認，現實裡不同學生是不同手機）
        res.append(devs[i].post("/api/checkin", data={"grant": grants[i], "student_id": ids[i], "name": "學生" + ids[i][-1], "pin": "1234", "pin2": "1234"},
                                headers={"user-agent": f"iPhone/{i}"}).json())
    ths = [threading.Thread(target=go, args=(i,)) for i in range(len(ids))]; [t.start() for t in ths]; [t.join() for t in ths]
    assert all(r["ok"] and r["status"] == "出席" for r in res) and h.state(sid)["counts"]["present"] == 4


def test_auto_finalize_posts_discord_and_skips_late_added(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?", ('{"discord_channel": "42"}', h.cid["CD"]))
    sid = h.open("CD")
    h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1")
    h.checkin(sid, "dB-1234567890123456", "9994A002", "學生2")
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,created_at) VALUES(?,?,?,?)", (h.cid["CD"], "9994A009", "後加", h.A.now()))
        con.execute("UPDATE sessions SET late_until=? WHERE id=?", (h.A.now() - 1, sid))
    h.T.get("/t")  # 觸發自動結案
    for _ in range(50):
        if sent: break
        time.sleep(0.05)
    assert sent and sent[0][0] == "42" and "後加" not in sent[0][1]
    d = h.db_rows(sid); assert "9994A009" not in d and d["9994A004"]["status"] == "absent"
    late = next(r for r in h.state(sid)["rows"] if r["student_id"] == "9994A009"); assert late["status"] == ""  # 即時名單列出加選生但不記缺席（可手動補登）
    with h.A.db() as con:
        assert con.execute("SELECT COUNT(*) FROM pin_fail WHERE session_id=?", (sid,)).fetchone()[0] == 0


def test_extend_in_late_phase_only_extends_late_window(h):
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET open_until=? WHERE id=?", (h.A.now() - 1, sid))
    j = h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1"); assert j["status"] == "遲到"
    with h.A.db() as con: before = dict(h.A.get_session(con, sid))
    r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2}).json(); assert r["phase"] == "late"
    with h.A.db() as con: after = dict(h.A.get_session(con, sid))
    assert after["open_until"] == before["open_until"] and after["late_until"] == before["late_until"] + 120
    assert h.checkin(sid, "dB-1234567890123456", "9994A002", "學生2")["status"] == "遲到"  # 晚到的人仍是遲到


def test_close_delete_race_leaves_no_orphans(h):
    for _ in range(6):
        sid = h.open("CD"); res = []
        ths = [threading.Thread(target=lambda: res.append(h.T.post(f"/api/t/session/{sid}/close").status_code)),
               threading.Thread(target=lambda: res.append(h.T.post(f"/api/t/session/{sid}/delete").status_code))]
        [t.start() for t in ths]; [t.join() for t in ths]
    with h.A.db() as con:
        orphans = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id NOT IN (SELECT id FROM sessions)").fetchone()[0]
    assert orphans == 0


def test_device_cookie_is_server_issued(h):
    sid = h.open("CD"); S = h.student()
    r = S.get("/c"); ck = r.headers.get("set-cookie", ""); assert "attend_dk" in ck and "httponly" in ck.lower()
    g = h.grant(sid, S)
    j = S.post("/api/checkin", data={"grant": g, "device_key": "attacker-supplied-xxxxxxxxxx", "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234"}).json()
    assert j["ok"]
    # 前端自報的 device_key 完全不採：換一個自報值仍是同一支手機
    j = S.post("/api/checkin", data={"grant": h.grant(sid, S), "device_key": "another-xxxxxxxxxxxxxxxx", "student_id": "9994A001", "pin": "1234"}).json()
    assert j["ok"] and j["again"]
    assert S.post("/api/whoami", data={"course_id": h.cid["CD"]}).json()["known"] is True
    assert S.post("/api/me").json()["courses"][0]["student_id"] == "9994A001"
    assert h.student().post("/api/whoami", data={"course_id": h.cid["CD"]}).json()["known"] is False  # 沒 cookie 的 client 什麼都不認


def test_proxy_all_unbound_students_from_one_client_is_flagged(h):
    """一個不留 cookie 的攻擊者替全班簽：每次拿到新 cookie＝新裝置，但同來源多筆首綁會被標記；
    同指紋的第二人起一般點名轉待確認（第三筆起另標 bind_burst）。"""
    sid = h.open("CD"); results = []
    for stu, name in (("9994A001", "學生1"), ("9994A002", "學生2"), ("9994A003", "學生3"), ("9994A004", "學生4")):
        S = h.student()  # 每次全新 client（等同不回送 cookie）
        g = h.grant(sid, S)
        results.append(S.post("/api/checkin", data={"grant": g, "student_id": stu, "name": name, "pin": "1234", "pin2": "1234"}, headers={"user-agent": "bot", "cf-connecting-ip": "203.0.113.9"}).json())
    st = [r["status"] for r in results]
    assert st == ["出席", "待確認", "待確認", "待確認"], st  # 同指紋第二人起轉待確認；bind_burst 第三筆起標記
    with h.A.db() as con:
        flagged = [r["student_id"] for r in con.execute("SELECT st.student_id FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=? AND k.flags LIKE '%bind_burst%' ORDER BY st.student_id", (sid,))]
    assert flagged == ["9994A003", "9994A004"]
    with h.A.db() as con:
        same_fp = [r[0] for r in con.execute("SELECT st.student_id FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=? AND k.flags LIKE '%same_fp%' ORDER BY st.student_id", (sid,))]
    assert same_fp == ["9994A001", "9994A002", "9994A003", "9994A004"], same_fp  # 先簽的那位被孿生規則一併標記


def test_grant_race_two_devices(h):
    sid = h.open("CD"); A, B = h.dev("raceA"), h.dev("raceB"); g = h.grant(sid, A); res = []
    def go(cl, stu, name):
        res.append(cl.post("/api/checkin", data={"grant": g, "student_id": stu, "name": name, "pin": "1234", "pin2": "1234"}).status_code)
    ths = [threading.Thread(target=go, args=(A, "9994A001", "學生1")), threading.Thread(target=go, args=(B, "9994A002", "學生2"))]
    [t.start() for t in ths]; [t.join() for t in ths]
    assert sorted(res) == [200, 410]


def test_teacher_ip_first_only_and_force(h):
    sid = h.open("CD")
    assert h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "140.131.1.1"}).json()["ip"] == "140.131.1.1"
    assert h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "10.9.9.9"}).json().get("kept") is True  # 手機 4G 開投影頁不會蓋掉
    assert h.T.post(f"/api/t/session/{sid}/teacher-ip", data={"force": "1"}, headers={"cf-connecting-ip": "10.9.9.9"}).json()["ip"] == "10.9.9.9"


def test_late_added_student_can_be_backfilled(h):
    sid = h.open("CD"); h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,created_at) VALUES(?,?,?,?)", (h.cid["CD"], "9994A009", "後加", h.A.now()))
    pk = h.pk("CD", "9994A009")
    assert any(r["student_id"] == "9994A009" for r in h.state(sid)["rows"])  # 即時名單列出，才能手動補登
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    with h.A.db() as con:
        sessions, rows = h.A.build_report(con, h.cid["CD"]); ch, txt = h.A.session_summary(con, sid)
    assert next(r for r in rows if r["stu"]["student_id"] == "9994A009")["cells"] == ["present"]
    assert "全班 5 人" in txt.splitlines()[0]


def test_manual_keeps_qr_time_and_flags_qr_after_manual(h):
    sid = h.open("CD"); h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1")
    ts_before = h.row(sid, "9994A001")["ts"]; assert ts_before
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "late"})
    assert h.row(sid, "9994A001")["ts"] == ts_before  # 老師裁定不會抹掉真正的簽到時間
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A002"), "status": "excused"})
    assert h.row(sid, "9994A002")["ts"] == ""
    j = h.checkin(sid, "dB-1234567890123456", "9994A002", "學生2"); assert j["again"] and j["status"] == "請假"
    assert "qr_after_manual" in h.row(sid, "9994A002")["flags"]


def test_settings_invalid_value_never_500(h):
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings='{\"spot_n\": \"abc\", \"allow_bind\": \"nope\", \"discord_channel\": 12}' WHERE id=?", (h.cid["CD"],))
    assert h.T.get("/t").status_code == 200 and h.T.get(f"/t/course/{h.cid['CD']}/students").status_code == 200
    r = h.T.post(f"/t/course/{h.cid['CD']}/settings", data={"open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 5, "spot_n": 3, "ip_check": "1", "discord_channel": "abc123"})
    assert r.status_code == 400


def test_projector_get_does_not_write_ip_but_post_does(h):
    sid = h.open("CD")
    h.T.get(f"/t/projector/{sid}", headers={"cf-connecting-ip": "1.1.1.1"})
    with h.A.db() as con: assert h.A.get_session(con, sid)["teacher_ip"] is None
    assert h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "140.131.1.1"}).json()["ip"] == "140.131.1.1"
    with h.A.db() as con: assert h.A.get_session(con, sid)["teacher_ip"] == "140.131.1.1"


def test_fullwidth_pin_rejected(h):
    sid = h.open("CD")
    j = h.checkin(sid, "dA-1234567890123456", "9994A001", "學生1", pin="１２３４"); assert j["ok"] is False and "PIN" in j["error"]


# 學生頁倒數要看場次什麼時候結束

def _student_entry_url(h, sid):
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        st = h.A.course_settings(h.A.get_course(con, s["course_id"]))
    slot = int(h.A.now() // st["qr_interval"])
    return f"/s/{sid}/{slot}/{h.A.slot_sig(s, slot)}"


def test_landing_exposes_session_left_one_minute_before_late_until(h):
    import re
    import time_machine
    sid = h.open("CD")
    with h.A.db() as con:
        late_until = h.A.get_session(con, sid)["late_until"]
    with time_machine.travel(late_until - 60, tick=False):
        r = h.dev("dev6-new").get(_student_entry_url(h, sid), headers={"user-agent": "iPhone"})
        assert r.status_code == 200
        m = re.search(r"SESSION_LEFT=(\d+)", r.text)
        assert m, "落地頁沒有 SESSION_LEFT"
        assert 55 <= int(m.group(1)) <= 60, m.group(0)


def test_banner_path_also_exposes_session_left(h):
    import re
    import time_machine
    sid = h.open("CD")
    assert h.checkin(sid, "dev6-bound", "9994A001", name="學生1", pin="1234")["ok"]
    with h.A.db() as con:
        late_until = h.A.get_session(con, sid)["late_until"]
    with time_machine.travel(late_until - 60, tick=False):
        r = h.dev("dev6-bound").get(_student_entry_url(h, sid), headers={"user-agent": "iPhone"})
        assert "這一場已經簽到了" in r.text
        m = re.search(r"SESSION_LEFT=(\d+)", r.text)
        assert m and 55 <= int(m.group(1)) <= 60, r.text[-300:]


def test_template_countdown_texts(h):
    from pathlib import Path
    text = (Path(h.A.__file__).parent / "templates" / "student.html").read_text(encoding="utf-8")
    assert "sessDeadline=Date.now()+SESSION_LEFT*1000" in text
    assert "'點名再 '+sleft+' 秒結束，請在那之前送出'" in text
    assert "'點名時間到了，還沒送出的請馬上按簽到；被擋就舉手找老師'" in text


# api_state 的 filling（還在填表的手機數）

def test_state_filling_counts_landed_but_not_signed_devices(h):
    import re
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        url = _student_entry_url(h, sid)
        for dev in ("dev7-a", "dev7-b"):
            r = h.dev(dev).get(url, headers={"user-agent": "iPhone"})
            assert r.status_code == 200
        grant = re.search(r'GRANT="([^"]+)"', h.dev("dev7-a").get(url, headers={"user-agent": "iPhone"}).text).group(1)
        j = h.dev("dev7-a").post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                                       "pin": "1234", "pin2": "1234", "hints": "h"}).json()
        assert j["ok"] is True, j
        st = h.state(sid)
        assert st["filling"] == 1, st["filling"]
        assert "open_until" in st and "now" in st
        with h.A.db() as con:
            gs = h.A.course_settings(h.A.get_course(con, h.cid["CD"]))["grant_seconds"]
        tr.shift(gs + 5)
        assert h.state(sid)["filling"] == 0


def test_close_confirm_text_in_session_template(h):
    from pathlib import Path
    text = (Path(h.A.__file__).parent / "templates" / "session.html").read_text(encoding="utf-8")
    assert "let m='結束點名？沒簽的會記缺席。';" in text
    assert "'\\n現在還有 '+state.filling+' 支手機在填表（三分鐘內掃到、還沒簽到成功），現在結束他們會記缺席。'" in text
    assert "'\\n出席窗還要 '+Math.max(1,Math.ceil((state.open_until-state.now)/60))+' 分鐘才關。'" in text


# 樣板與文案

def test_help_page_has_new_rows_and_text(client):
    text = client.get("/help").text
    assert "這個學號已經設過 PIN" in text and "已被老師記為" in text
    assert "用最近一次簽到成功的那支手機或瀏覽器開" in text
    assert "第一次簽的人、或老師今天之前重設過你 PIN 的人" in text


def test_not_match_text_keeps_roster_mismatch_phrase(h):
    sid = h.open("CD")
    r = h.dev("dev10-nm").post("/api/checkin", data={"grant": h.grant(sid, h.dev("dev10-nm")), "student_id": "9999999",
                                                    "name": "x", "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 400
    assert r.json()["error"] == ("學號或姓名與【脈絡設計】的名冊不符。請先確認學號沒打錯、姓名照名冊寫全名；"
                                 "用六位數輸到別班的，請回上一頁重輸；剛加選的請找老師"), r.text


def test_student_and_base_templates(h):
    from pathlib import Path
    tpl = Path(h.A.__file__).parent / "templates"
    st = (tpl / "student.html").read_text(encoding="utf-8")
    assert 'onclick="notMe();return false"' in st and "function notMe(){" in st
    assert "這支手機綁的是別人的學號。借用別人的手機簽到會列為待確認，請下課前找老師確認；你自己的手機在身上的話，請用自己的手機掃。" in st
    assert "' 這一場只做綁定、不算出缺勤；今天還沒簽到成功的，請找老師補登。'" in st
    assert ("這支手機已綁定你的學號。這一場只做綁定、不算出缺勤；今天還沒簽到成功的，請找老師補登。"
            "下次上課掃碼後按一下就完成簽到。") in st
    assert "input.mono{font-size:16px}" in (tpl / "base.html").read_text(encoding="utf-8")


# 誤按結束點名 → 取消結束
# 時間一律用 time-machine 控制，不 sleep。

def _reopen(h, sid):
    return h.T.post(f"/api/t/session/{sid}/reopen")


def _sess(h, sid):
    with h.A.db() as con:
        return dict(h.A.get_session(con, sid))


def test_1_reopen_removes_auto_absent_keeps_signed_and_teacher_edits(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False):
        sid = h.open("CD")
        assert h.checkin(sid, "dev5a-1", "9994A001", name="學生1", pin="1234")["ok"]
        assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
        rows = h.db_rows(sid)
        assert rows["9994A002"]["source"] == "auto" and rows["9994A003"]["source"] == "auto"
        assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A003"), "status": "excused"}).status_code == 200
        r = _reopen(h, sid)
        assert r.status_code == 200, r.text
        j = r.json()
        # 夾具名冊是四個人：甲簽到、丙改請假（source 變 manual 不刪），所以乙與丁兩筆 auto 缺席被拿掉
        assert j["ok"] is True and j["removed"] == 2 and j["discord_sent"] is False, j
        rows = h.db_rows(sid)
        assert rows["9994A001"]["status"] == "present"
        assert rows["9994A003"]["status"] == "excused" and rows["9994A003"]["source"] == "manual"
        assert "9994A002" not in rows and "9994A004" not in rows
        assert _sess(h, sid)["closed_at"] is None
        assert h.checkin(sid, "dev5a-2", "9994A002", name="學生2", pin="1234")["ok"] is True


def test_2_reopen_after_attendance_window_cut_by_mistaken_close_extends_open_until(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        s0 = _sess(h, sid)
        tr.shift(30)  # 出席窗內誤按結束
        assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
        assert _sess(h, sid)["closed_at"] < s0["open_until"]
        tr.shift(s0["open_until"] - h.A.now() + 20)  # 過了原本的 open_until 才取消
        t = h.A.now()
        assert t > s0["open_until"]
        assert _reopen(h, sid).status_code == 200
        s1 = _sess(h, sid)
        assert abs(s1["open_until"] - (t + 120)) < 1, (s1["open_until"], t)
        assert s1["late_until"] >= s1["open_until"]
        j = h.checkin(sid, "dev5b-1", "9994A002", name="學生2", pin="1234")
        assert j["ok"] and j["status"] == "出席", j


def test_3_reopen_after_close_in_late_window_keeps_open_until(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        s0 = _sess(h, sid)
        tr.shift(s0["open_until"] - h.A.now() + 10)  # 遲到窗內才結束
        assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
        tr.shift(10)
        assert _reopen(h, sid).status_code == 200
        s1 = _sess(h, sid)
        assert s1["open_until"] == s0["open_until"], "遲到窗結束的，出席窗原本就已經照時間關了，不能再延長"
        assert s1["late_until"] == s0["late_until"]
        j = h.checkin(sid, "dev5c-1", "9994A002", name="學生2", pin="1234")
        assert j["ok"] and j["status"] == "遲到", j


def test_3b_reopen_after_auto_close_at_late_until_extends_late_window_only(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        s0 = _sess(h, sid)
        tr.shift(s0["late_until"] - h.A.now() + 1)
        h.state(sid)  # 老師頁輪詢觸發自動結案（closed_at＝late_until）
        assert _sess(h, sid)["closed_at"] == s0["late_until"]
        tr.shift(100)
        t = h.A.now()
        assert _reopen(h, sid).status_code == 200
        s1 = _sess(h, sid)
        assert s1["open_until"] == s0["open_until"]
        assert abs(s1["late_until"] - (t + 120)) < 1, (s1["late_until"], t)


def test_4_reopen_refusals_with_exact_messages(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        r = _reopen(h, sid)  # 沒結束
        assert r.status_code == 409 and r.json()["error"] == "這一場還沒結束，不用取消。", r.text
        h.T.post(f"/api/t/session/{sid}/close")
        tr.shift(601)
        r = _reopen(h, sid)  # 超過 10 分鐘
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "結束超過 10 分鐘，不能取消結束了。晚到的人請在名單上改狀態，或用點名頁的「手動簽到」補登。", r.text
        assert _sess(h, sid)["closed_at"] is not None
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid2 = h.open("CD")
        h.T.post(f"/api/t/session/{sid2}/close")
        assert h.T.post(f"/api/t/session/{sid2}/void", data={"void": "1"}).status_code == 200
        r = _reopen(h, sid2)  # 已作廢
        assert r.status_code == 409 and r.json()["error"] == "這一場已作廢，不能取消結束。", r.text
        h.T.post(f"/api/t/session/{sid2}/void", data={"void": "0"})
        tr.shift(5)  # 新的一場的 opened_at 要比這一場晚（凍結的時鐘下兩場會同一秒）
        sid3 = h.open("CD")  # 之後開了新的一場
        assert sid3 != sid2
        r = _reopen(h, sid2)
        assert r.status_code == 409 and r.json()["error"] == "這門課之後已經開了新的一場，不能取消結束這一場。", r.text


def test_4b_reopen_requires_teacher_login(h):
    sid = h.open("CD")
    assert h.student().post(f"/api/t/session/{sid}/reopen").status_code == 401


# 突變測試找到的兩個邊界（do_reopen_session 的兩個邊界突變會存活）。正式站的時間戳是浮點數，
# 實際上幾乎不會剛好相等；這裡用整數時鐘把規格的字面邊界釘住：「10 分鐘內」含第 600 秒、「切斷」是 closed_at < open_until。

def test_4c_reopen_still_allowed_at_exactly_ten_minutes(h):
    import time_machine
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        h.T.post(f"/api/t/session/{sid}/close")
        tr.shift(600)
        assert h.A.now() - _sess(h, sid)["closed_at"] == 600
        r = _reopen(h, sid)
        assert r.status_code == 200, r.text


def test_5_close_exactly_at_open_until_is_not_a_cut_attendance_window(h):
    import time_machine
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        s0 = _sess(h, sid)
        tr.shift(s0["open_until"] - h.A.now())
        assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
        assert _sess(h, sid)["closed_at"] == s0["open_until"]
        tr.shift(10)
        assert _reopen(h, sid).status_code == 200
        s1 = _sess(h, sid)
        assert s1["open_until"] == s0["open_until"], "出席窗在結束那一刻已經照時間走完，不算被切斷，不延長"
        j = h.checkin(sid, "dev13e-1", "9994A002", name="學生2", pin="1234")
        assert j["ok"] and j["status"] == "遲到", j


def test_5_close_again_after_reopen_marks_unsigned_absent_again_and_logs_event(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False):
        sid = h.open("CD")
        assert h.checkin(sid, "dev5e-1", "9994A001", name="學生1", pin="1234")["ok"]
        h.T.post(f"/api/t/session/{sid}/close")
        assert _reopen(h, sid).status_code == 200
        h.T.post(f"/api/t/session/{sid}/close")
        rows = h.db_rows(sid)
        assert rows["9994A002"]["status"] == "absent" and rows["9994A002"]["source"] == "auto"
        with h.A.db() as con:
            ev = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='reopen'", (sid,)).fetchall()
        assert [e["reason"] for e in ev] == [f"3｜原本 {h.A.fmt(_sess(h, sid)['closed_at'])} 結束"], [dict(e) for e in ev]


def test_6_state_reopen_left(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        assert h.state(sid)["reopen_left"] == 0  # 還沒結束
        h.T.post(f"/api/t/session/{sid}/close")
        left = h.state(sid)["reopen_left"]
        assert isinstance(left, int) and 590 <= left <= 600, left
        tr.shift(601)
        assert h.state(sid)["reopen_left"] == 0
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid2 = h.open("CD")
        h.T.post(f"/api/t/session/{sid2}/close")
        h.T.post(f"/api/t/session/{sid2}/void", data={"void": "1"})
        assert h.state(sid2)["reopen_left"] == 0, "已作廢"
        h.T.post(f"/api/t/session/{sid2}/void", data={"void": "0"})
        assert h.state(sid2)["reopen_left"] > 0
        tr.shift(5)
        h.open("CD")
        assert h.state(sid2)["reopen_left"] == 0, "之後開了新場次"


def test_7_reopen_clears_auto_discord_hold_but_not_manual_hold_and_reports_discord_sent(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False):
        sid = h.open("CD")
        h.T.post(f"/api/t/session/{sid}/close")
        with h.A.db() as con:
            con.execute("UPDATE sessions SET note='discord_held' WHERE id=?", (sid,))
        assert _reopen(h, sid).json()["discord_sent"] is False
        assert _sess(h, sid)["note"] == ""
        h.T.post(f"/api/t/session/{sid}/close")
        with h.A.db() as con:
            con.execute("UPDATE sessions SET note='discord_held_manual' WHERE id=?", (sid,))
            con.execute("INSERT INTO events(ts, session_id, kind, reason) VALUES(?,?,?,?)", (h.A.now(), sid, "discord_sent", "auto"))
        j = _reopen(h, sid).json()
        assert _sess(h, sid)["note"] == "discord_held_manual"
        assert j["discord_sent"] is True, j


# 結束後才改的狀態不會更新到已發出的 Discord 名單

def _poll(cond, timeout=10.0):
    """等條件成立（輪詢到逾時為止），不用固定 sleep 猜時間。"""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)  # 輪詢間隔，不是等待依據
    return cond()


def _discord_sent_events(h, sid):
    with h.A.db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE session_id=? AND kind='discord_sent' ORDER BY id", (sid,))]


def test_discord_stale_counts_status_changes_after_last_send(h, monkeypatch):
    import json as _json
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?", (_json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    for i, stu in enumerate(("9994A001", "9994A002", "9994A003")):
        assert h.checkin(sid, f"dev8-{i}", stu, name=f"學生{i + 1}", pin="1234")["ok"]
    assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True  # 3/4 出席，不暫緩
    assert _poll(lambda: len(_discord_sent_events(h, sid)) == 1), "背景發送成功後要記一筆 discord_sent"
    assert _discord_sent_events(h, sid)[0]["reason"] == "auto"
    assert h.state(sid)["discord_stale"] == 0
    last = _discord_sent_events(h, sid)[0]["ts"]
    assert _poll(lambda: h.A.now() > last)  # 事件時間要嚴格大於上一次發送，等時鐘走過去，不猜秒數
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A003"), "status": "excused"}).status_code == 200
    assert h.state(sid)["discord_stale"] == 1
    assert h.T.post(f"/api/t/session/{sid}/notify").json()["ok"] is True
    ev = _discord_sent_events(h, sid)
    assert len(ev) == 2 and ev[1]["reason"] == "manual", ev
    assert h.state(sid)["discord_stale"] == 0


def test_discord_stale_is_zero_when_never_sent(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A003"), "status": "excused"})
    assert h.state(sid)["discord_stale"] == 0


# 被擋在查學號之前的拒絕要歸戶

def _gate_setup(h, dev):
    """開一場、拿一份有效 grant（綁在 dev 這支手機上）。回 (sid, grant)。"""
    sid = h.open("CD")
    cl = h.dev(dev)
    return sid, h.grant(sid, cl)


def _gate_post(h, dev, grant, stu="9994A001", name="", pin="1234", pin2="1234"):
    return h.dev(dev).post("/api/checkin", data={"grant": grant, "student_id": stu, "name": name, "pin": pin, "pin2": pin2, "hints": "h"},
                           headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})


def _last_reject(h, sid):
    with h.A.db() as con:
        r = con.execute("SELECT reason, student_pk FROM events WHERE session_id=? AND kind='checkin_reject' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    return dict(r) if r else None


def test_closed_reject_is_attributed_to_claimed_student(h):
    sid, grant = _gate_setup(h, "gate-a")
    h.T.post(f"/api/t/session/{sid}/close")
    r = _gate_post(h, "gate-a", grant, name="學生1")
    assert r.status_code == 410, r.text
    assert _last_reject(h, sid) == {"reason": "closed", "student_pk": h.pk("CD", "9994A001")}


def test_name_of_someone_else_is_not_attributed(h):
    sid, grant = _gate_setup(h, "gate-b")
    h.T.post(f"/api/t/session/{sid}/close")
    assert _gate_post(h, "gate-b", grant, name="王大明").status_code == 410
    assert _last_reject(h, sid) == {"reason": "closed", "student_pk": None}


def test_unknown_student_id_is_not_attributed(h):
    sid, grant = _gate_setup(h, "dev3b2")
    h.T.post(f"/api/t/session/{sid}/close")
    assert _gate_post(h, "dev3b2", grant, stu="9999999").status_code == 410
    assert _last_reject(h, sid) == {"reason": "closed", "student_pk": None}


def test_tampered_grant_is_not_attributed(h):
    sid, grant = _gate_setup(h, "gate-c")
    h.T.post(f"/api/t/session/{sid}/close")
    bad = grant[:-1] + ("0" if grant[-1] != "0" else "1")
    assert _gate_post(h, "gate-c", bad, name="學生1").status_code == 410
    last = _last_reject(h, sid)
    assert last is not None and last["student_pk"] is None, last


def test_expired_grant_with_valid_signature_is_attributed(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid, grant = _gate_setup(h, "gate-d")
        with h.A.db() as con:
            gs = h.A.course_settings(h.A.get_course(con, h.cid["CD"]))["grant_seconds"]
        tr.shift(gs + 5)
        r = _gate_post(h, "gate-d", grant, name="學生1")
        assert r.status_code == 410, r.text
        assert _last_reject(h, sid) == {"reason": "grant_expired", "student_pk": h.pk("CD", "9994A001")}


def test_no_cookie_other_device_and_throttle_rejects_are_attributed(h):
    sid, grant = _gate_setup(h, "gate-e")
    # 沒有裝置 cookie 的人拿著有效 grant 送出
    r = h.student().post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 400, r.text
    assert _last_reject(h, sid) == {"reason": "no_cookie", "student_pk": h.pk("CD", "9994A001")}
    # 別支手機拿這份 grant（憑證綁的是另一支）
    h.dev("dev3e-other")
    assert _gate_post(h, "dev3e-other", grant, name="學生1").status_code == 410
    assert _last_reject(h, sid) == {"reason": "grant_other_device", "student_pk": h.pk("CD", "9994A001")}
    # 一支手機一分鐘 12 次
    for _ in range(12):
        _gate_post(h, "gate-e", grant, name="王大明", pin="1234")
    r = _gate_post(h, "gate-e", grant, name="學生1")
    assert r.status_code == 429, r.text
    assert _last_reject(h, sid) == {"reason": "throttle", "student_pk": h.pk("CD", "9994A001")}


def test_checkin_error_is_attributed(h, monkeypatch):
    import pytest
    sid, grant = _gate_setup(h, "gate-f")
    monkeypatch.setattr(h.A, "course_settings", lambda row: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        _gate_post(h, "gate-f", grant, name="學生1")
    with h.A.db() as con:
        r = con.execute("SELECT student_pk FROM events WHERE session_id=? AND kind='checkin_error'", (sid,)).fetchone()
    assert r is not None and r["student_pk"] == h.pk("CD", "9994A001"), r


def test_claimed_pk_never_raises_when_database_is_locked(h, monkeypatch):
    import sqlite3
    sid = h.open("CD")
    assert h.A._claimed_pk(sid, "9994A001", "學生1") == h.pk("CD", "9994A001")
    assert h.A._claimed_pk(sid, "９９９４ａ００１ ", "") == h.pk("CD", "9994A001"), "全形半形、大小寫與空白跟 do_checkin 用同一套正規化"
    assert h.A._claimed_pk(sid, "9994A001", "王大明") is None
    assert h.A._claimed_pk(sid, "9999999", "") is None
    assert h.A._claimed_pk(999999, "9994A001", "") is None, "場次不存在"

    def _locked(timeout=10):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(h.A, "connect", _locked)
    assert h.A._claimed_pk(sid, "9994A001", "學生1") is None


def test_landing_after_close_logs_land_closed_not_land_ok(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.dev("gate-g").get(_student_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200
    with h.A.db() as con:
        kinds = [e["kind"] for e in con.execute("SELECT kind FROM events WHERE session_id=? AND kind LIKE 'land%'", (sid,))]
    assert kinds == ["land_closed"], kinds
    assert h.state(sid)["funnel"]["total"]["land_ok"] == 0


# beacon 限流與 beacon_dropped

def _beacon(client, key, ip, sid=""):
    return client.post("/api/beacon", data={"kind": "neterr", "msg": "x", "sid": sid},
                       headers={"cookie": f"attend_dk={key}", "cf-connecting-ip": ip})


def _count_events(h, kind, **where):
    q, args = "SELECT reason, session_id FROM events WHERE kind=?", [kind]
    for k, v in where.items():
        q += f" AND {k}=?"
        args.append(v)
    with h.A.db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def test_beacon_ip_limit_is_240_and_logs_one_beacon_dropped(h):
    sid = h.open("CD")
    cl = h.student()
    for i in range(250):
        assert _beacon(cl, f"ipdev{i:020d}", "10.9.9.9", str(sid)).status_code == 204
    assert len(_count_events(h, "client")) == 240
    dropped = _count_events(h, "beacon_dropped")
    assert len(dropped) == 1 and dropped[0]["reason"] == "ip" and dropped[0]["session_id"] == sid, dropped


def test_beacon_device_limit_logs_one_beacon_dropped_device(h):
    sid = h.open("CD")
    cl = h.student()
    for _ in range(10):
        assert _beacon(cl, "samedev0000000000000001", "10.8.8.8", str(sid)).status_code == 204
    assert len(_count_events(h, "client")) == 6
    dropped = _count_events(h, "beacon_dropped")
    assert len(dropped) == 1 and dropped[0]["reason"] == "device" and dropped[0]["session_id"] == sid, dropped


# 「刪除此次」不連老師的操作紀錄一起刪

def test_delete_session_keeps_unbind_event_so_reset_exemption_survives(h):
    sid = h.open("CD")
    assert h.checkin(sid, "dev4-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    pk = h.pk("CD", "9994A001")
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"}).status_code == 200  # 進行中的場次，事件帶著 sid
    assert h.T.post(f"/api/t/session/{sid}/delete").json()["ok"] is True
    sid2 = h.open("CD")
    r = h.dev("dev4-new").post("/api/checkin", data={"grant": h.grant(sid2, h.dev("dev4-new")), "student_id": "9994A001", "name": "學生1",
                                                       "pin": "5555", "pin2": "5555", "hints": "h"},
                               headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})
    assert r.status_code == 200, r.text
    with h.A.db() as con:
        unb = con.execute("SELECT session_id FROM events WHERE kind='unbind' AND student_pk=?", (pk,)).fetchall()
        dele = con.execute("SELECT session_id, reason FROM events WHERE kind='session_deleted'").fetchall()
    assert len(unb) == 1 and unb[0]["session_id"] is None, [dict(x) for x in unb]
    assert len(dele) == 1 and dele[0]["session_id"] is None, [dict(x) for x in dele]
    assert dele[0]["reason"] == f"course:{h.cid['CD']} sid:{sid} rows:1", dict(dele[0])


def test_allow_bind_event_also_survives_and_qr_returns_opened_at(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    h.T.post(f"/api/t/session/{sid}/delete")
    with h.A.db() as con:
        ev = con.execute("SELECT session_id FROM events WHERE kind='allow_bind'").fetchall()
    assert len(ev) == 1 and ev[0]["session_id"] is None, [dict(x) for x in ev]
    sid2 = h.open("CD")
    j = h.T.get(f"/api/t/session/{sid2}/qr").json()
    assert j["opened_at"] == _sess(h, sid2)["opened_at"], j


# 解除綁定／重設的回傳旗標

def test_unbind_returns_reset_and_revoked_today_flags(h):
    pk = h.pk("CD", "9994A001")
    j = h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""}).json()
    assert j == {"ok": True, "reset": False, "revoked_today": False}, j
    j = h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"}).json()
    assert j == {"ok": True, "reset": True, "revoked_today": False}, j
    sid = h.open("CD", kind="bind")
    assert h.checkin(sid, "dev9-A", "9994A001", name="學生1", pin="9999")["ok"] is True
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    assert h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"}).json()["unbound"] is True
    j = h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""}).json()
    assert j["revoked_today"] is True and j["reset"] is False, j
    other = h.T.post(f"/api/t/student/{h.pk('CD', '9994A002')}/unbind", data={"reset_pin": ""}).json()
    assert other["revoked_today"] is False, "判不在的是別人，不是這個學號"


# 操作手冊文字

def test_home_manual_texts(h):
    text = h.T.get("/t").text
    assert "代綁的人設的 PIN 還在" in text
    assert ("請假的人先在名單上改成「請假」再按結束；結束後才改的，按「發到 Discord」更新名單（舊訊息撤不回）。"
            "誤按結束：10 分鐘內按「取消結束」，已簽到的都會保留。誤開的點名才用「刪除此次」。") in text
    assert "誤開或誤按結束，都用「刪除此次」整筆撤掉再重開" not in text
    assert "把不該綁的那個「解除綁定」" not in text


# 前端文字
# 這幾項改在樣板的 JavaScript 裡，單元層沒有瀏覽器，用「伺服器吐出來的頁面文字」守字句；
# 真的點下去的行為由 e2e_teacher_friction_playwright.py／e2e_friction_playwright.py 驗。

def test_students_confirm_names_who_and_says_what_happens(h):
    t = h.T.get("/t/course/1/students").text
    assert "btn.dataset.sid+' '+btn.dataset.name" in t
    assert "會清掉他綁定的手機和 PIN，他要重新設 PIN 才能簽到。" in t
    assert "PIN 會留著，他用新手機輸入原本的 PIN 就能綁。" in t
    assert 'data-sid="9994A001"' in t and "onclick=\"unbind(" in t


def test_students_alert_texts_by_flag(h):
    t = h.T.get("/t/course/1/students").text
    assert "已解除。這個學號今天被抽點判不在，今天不能再綁" in t
    assert "已重設。他今天重新掃 QR、設新的 PIN 就能綁（不受首次綁定開關限制）；過了今天，首次綁定要開著才能綁。" in t
    assert "已解除。他之後掃 QR、輸入原本的 PIN 就會綁到那支手機（不受首次綁定開關限制）。忘了 PIN 的話請改按「重設」；他這堂課已經 PIN 錯太多次被鎖的話，請在點名頁用「手動簽到」補登。" in t
    assert "已解除。他今天可以直接重新掃 QR 綁定（不受首次綁定開關限制）。" not in t


def test_spot_absent_asks_before_posting(h):
    s = h.open("CD")
    t = h.T.get(f"/t/session/{s}").text
    body = t.split("function spotResult", 1)[1][:900]
    assert "p===0" in body and "confirm(" in body
    assert "確定不在？" in body and "state.kind==='bind'" in body


def test_session_page_has_reopen_button_and_texts(h):
    s = h.open("CD")
    t = h.T.get(f"/t/session/{s}").text
    assert 'id="reopenBtn"' in t and "取消結束" in t
    assert "async function reopenSession" in t and "/reopen" in t
    assert 'id="discordStaleMsg"' in t


def test_base_has_beacon_queue_and_student_sets_sid():
    base = (ROOT_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    assert "attendBeaconQ" in base and "window.attendFlush" in base and "window.ATTEND_SID" in base
    assert "addEventListener('online'" in base and "addEventListener('pageshow'" in base
    stu = (ROOT_DIR / "templates" / "student.html").read_text(encoding="utf-8")
    assert "window.ATTEND_SID={{ sid" in stu and "attendFlush()" in stu


def test_session_roster_folds_on_narrow_screens():
    t = (ROOT_DIR / "templates" / "session.html").read_text(encoding="utf-8")
    media = t.split("@media (max-width:600px)", 1)[1].split("</style>", 1)[0]
    assert "#rows tr{display:grid" in media and "#rowsTable thead{display:none}" in media
    assert "#rows td.hide-sm{display:block}" in media


def test_student_page_carries_sid_for_the_beacon(h):
    s = h.open("CD")
    code = h.T.get(f"/api/t/session/{s}/qr").json()["code"]
    page = h.student().post("/c", data={"code": code})
    assert page.status_code == 200
    assert f"window.ATTEND_SID={s};" in page.text


# 背景執行緒與課程設定輔助

def _poll_until(cond, tries=500):
    """輪詢實際條件（背景執行緒的結果），次數上限而不是牆鐘時間——測試裡時鐘可能被 time-machine 凍結。"""
    for _ in range(tries):
        if cond():
            return True
        time.sleep(0.02)  # 輪詢間隔，不是等待依據
    return cond()


def _course_settings(h, course, **kw):
    import json as _json
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?", (_json.dumps(kw), h.cid[course]))


def _fake_discord(h, monkeypatch, results=None):
    """假的 discord_post：results 是依序回傳的 (ok, msg) 清單，用完後一律成功；回傳呼叫記錄。"""
    calls = []
    seq = list(results or [])

    def fake(ch, c):
        calls.append(c)
        return seq.pop(0) if seq else (True, "HTTP 200")
    monkeypatch.setattr(h.A, "discord_post", fake)
    h.A.DISCORD_TOKEN = "x"
    return calls


# 取消結束保底 2 分鐘

def test_1_reopen_after_early_close_gives_two_minutes_of_attendance_window(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(120)  # 2:00 結束（出席窗 3 分）
        assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]
        tr.shift(50)  # 2:50 取消
        assert _reopen(h, sid).status_code == 200
        assert _sess(h, sid)["open_until"] - h.A.now() >= 120
        tr.shift(15)  # 3:05 送出
        j = h.checkin(sid, "reopen-a", "9994A001", name="學生1", pin="1234")
        assert j["ok"] and j["status"] == "出席", j


def test_2_reopen_near_late_until_gives_two_minutes_and_sends_discord_once(h, monkeypatch):
    import time_machine
    calls = _fake_discord(h, monkeypatch)
    _course_settings(h, "OOP", open_minutes=30, late_minutes=35, discord_channel="123456789012345")
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("OOP")
        j0 = h.checkin(sid, "dev13b-1", "999000025", name="清大5", pin="1234")
        assert j0["ok"], j0
        tr.shift(33 * 60)
        assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True
        assert _poll_until(lambda: len(calls) == 1)
        tr.shift(115)  # 34:55
        assert _reopen(h, sid).status_code == 200
        assert _sess(h, sid)["late_until"] - h.A.now() >= 120
        tr.shift(8)  # 35:03，原本的遲到上限已過
        with h.A.db() as con:
            h.A.finalize_expired(con)
        assert _sess(h, sid)["closed_at"] is None
        tr.shift(7)  # 35:10
        j = h.checkin(sid, "dev13b-2", "999000026", name="清大6", pin="1234")
        assert j["ok"] and j["status"] == "遲到", j
        assert len(calls) == 1, calls


def test_3_reopen_after_close_in_late_window_changes_nothing_when_time_is_ample(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        s0 = _sess(h, sid)
        tr.shift(300)  # 5:00 結束（已在遲到窗）
        h.T.post(f"/api/t/session/{sid}/close")
        tr.shift(30)  # 5:30 取消，遲到窗還剩 9.5 分鐘
        assert _reopen(h, sid).status_code == 200
        s1 = _sess(h, sid)
        assert s1["open_until"] == s0["open_until"] and s1["late_until"] == s0["late_until"], (s0, s1)


def test_4_bind_session_reopen_gets_two_minutes_for_both_windows(h):
    import time_machine
    _course_settings(h, "CD", bind_minutes=3)
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD", kind="bind")
        tr.shift(130)
        h.T.post(f"/api/t/session/{sid}/close")
        tr.shift(30)  # 結束後 30 秒取消，原本剩 20 秒
        s0 = _sess(h, sid)
        assert s0["late_until"] - h.A.now() <= 21
        assert _reopen(h, sid).status_code == 200
        s1 = _sess(h, sid)
        assert s1["open_until"] >= h.A.now() + 120 and s1["late_until"] >= h.A.now() + 120, s1
        assert s1["open_until"] == s1["late_until"], s1


# 回主頁按「開始點名」前的確認框

def test_home_start_button_confirms_when_session_just_closed(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        h.T.post(f"/api/t/session/{sid}/close")
        tr.shift(180)
        t = h.T.get("/t").text
        assert "上一場 3 分鐘前才結束" in t, t[-3000:]
        assert "確定要開新的一場？" in t and "取消結束" in t
        assert t.count("上一場 3 分鐘前才結束") == 1, "只有 CD 這門課有剛結束的場次，只做綁定那顆不加"
        tr.shift(480)  # 共 11 分鐘
        assert "上一場" not in h.T.get("/t").text


def test_voided_or_live_session_has_no_confirm(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        h.T.post(f"/api/t/session/{sid}/close")
        h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
        tr.shift(60)
        assert "上一場" not in h.T.get("/t").text, "作廢的不含"
        h.T.post(f"/api/t/session/{sid}/void", data={"void": "0"})
        assert "上一場 1 分鐘前才結束" in h.T.get("/t").text
        tr.shift(5)
        h.open("CD")  # 現在有進行中的場次
        assert "上一場" not in h.T.get("/t").text


# 取消結束後 Discord 失敗紅字不殘留

def _course(h, monkeypatch, results):
    calls = _fake_discord(h, monkeypatch, results)
    _course_settings(h, "CD", discord_channel="123456789012345")
    sid = h.open("CD")
    for i, stu in enumerate(("9994A001", "9994A002", "9994A003")):
        assert h.checkin(sid, f"dev11-{id(calls)}-{i}", stu, name=f"學生{i + 1}", pin="1234")["ok"]
    return sid, calls


def test_1_reopen_clears_discord_failed_and_second_success_keeps_it_clear(h, monkeypatch):
    sid, calls = _course(h, monkeypatch, [(False, "boom")])
    assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True
    assert _poll_until(lambda: _sess(h, sid)["note"] == "discord_failed")
    assert h.state(sid)["discord_failed"] is True
    assert _reopen(h, sid).status_code == 200
    assert h.state(sid)["discord_failed"] is False and _sess(h, sid)["note"] == ""
    assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True
    assert _poll_until(lambda: len(calls) == 2)
    assert _poll_until(lambda: len(_discord_sent_events(h, sid)) == 1)
    assert h.state(sid)["discord_failed"] is False and _sess(h, sid)["note"] == ""


def test_2_background_success_after_failure_clears_note(h, monkeypatch):
    sid, calls = _course(h, monkeypatch, [(False, "boom")])
    h.T.post(f"/api/t/session/{sid}/close")
    assert _poll_until(lambda: _sess(h, sid)["note"] == "discord_failed")
    h.A._bg_discord_post(sid, "123456789012345", "x")  # 第二次（重發）成功
    assert _sess(h, sid)["note"] == ""
    assert h.state(sid)["discord_failed"] is False


# filling 的算法

def _land_how(h, sid, dev, how):
    if how == "qr":
        r = h.dev(dev).get(_student_entry_url(h, sid), headers={"user-agent": "iPhone"})
        assert r.status_code == 200
    else:
        code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
        assert h.dev(dev).post("/c", data={"code": code}).status_code == 200


def test_1_filling_counts_qr_and_code_landings(h):
    sid = h.open("CD")
    _land_how(h, sid, "dev12a-qr", "qr")
    _land_how(h, sid, "dev12a-code", "code")
    assert h.state(sid)["filling"] == 2, h.state(sid)["filling"]


def test_2_filling_skips_device_blocked_by_already_recorded(h):
    sid = h.open("CD")
    assert h.checkin(sid, "dev12b-A", "9994A001", name="學生1", pin="1234")["ok"]
    _land_how(h, sid, "dev12b-B", "qr")
    assert h.state(sid)["filling"] == 1
    r = h.dev("dev12b-B").post("/api/checkin", data={"grant": h.grant(sid, h.dev("dev12b-B")), "student_id": "9994A001", "name": "",
                                                    "pin": "1234", "pin2": "", "hints": "h"})
    assert r.status_code == 409, r.text
    assert h.state(sid)["filling"] == 0, h.state(sid)["filling"]


def test_3_filling_skips_landing_whose_student_is_already_recorded_by_teacher(h):
    sid = h.open("CD")
    _land_how(h, sid, "dev12c-A", "qr")
    pk = h.pk("CD", "9994A002")
    with h.A.db() as con:  # 帶 student_pk 的落地事件（認得是誰的新環境）
        con.execute("INSERT INTO events(ts, session_id, kind, device8, student_pk) VALUES(?,?,?,?,?)",
                    (h.A.now(), sid, "land_ok", "deadbeef", pk))
    assert h.state(sid)["filling"] == 2
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"}).status_code == 200
    assert h.state(sid)["filling"] == 1


# 主頁 Discord 沒發出標籤

def test_home_shows_discord_failed_tag(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET note='discord_failed' WHERE id=?", (sid,))
    assert "Discord 沒發出" in h.T.get("/t").text
    with h.A.db() as con:
        con.execute("UPDATE sessions SET note='' WHERE id=?", (sid,))
    assert "Discord 沒發出" not in h.T.get("/t").text


# 去重回 again 時也算已發

def test_notify_again_counts_as_sent_so_stale_goes_to_zero(h, monkeypatch):
    import time_machine
    _fake_discord(h, monkeypatch)
    _course_settings(h, "CD", discord_channel="123456789012345")
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        for i, stu in enumerate(("9994A001", "9994A002", "9994A003")):
            assert h.checkin(sid, f"dev14-{i}", stu, name=f"學生{i + 1}", pin="1234")["ok"]
        h.T.post(f"/api/t/session/{sid}/close")
        assert _poll_until(lambda: len(_discord_sent_events(h, sid)) == 1)
        pk = h.pk("CD", "9994A004")
        tr.shift(5)
        h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "excused"})
        tr.shift(5)
        assert h.T.post(f"/api/t/session/{sid}/notify").json() == {"ok": True}
        tr.shift(5)
        h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "absent"})
        tr.shift(5)
        h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "excused"})  # 改回，名單其實跟剛發的一樣
        assert h.state(sid)["discord_stale"] == 2
        tr.shift(5)
        assert h.T.post(f"/api/t/session/{sid}/notify").json() == {"ok": True, "again": True}
        assert h.state(sid)["discord_stale"] == 0, h.state(sid)["discord_stale"]


# beacon 的 sid 是上標數字等 Unicode 數字

def test_beacon_unicode_digit_sid_does_not_500(h):
    cl = h.student()
    for sid in ("²", "١٢"):
        r = cl.post("/api/beacon", data={"kind": "neterr", "msg": "x", "sid": sid},
                    headers={"cookie": "attend_dk=unicodedev0000000000001", "cf-connecting-ip": "10.7.7.7"})
        assert r.status_code == 204, (sid, r.status_code)
    ev = _count_events(h, "client")
    assert len(ev) == 2 and all(e["session_id"] is None for e in ev), ev


# 解除綁定提示提到本場 PIN 鎖

def test_students_unbind_alert_mentions_pin_lock(h):
    t = h.T.get("/t/course/1/students").text
    assert "已解除。他之後掃 QR、輸入原本的 PIN 就會綁到那支手機（不受首次綁定開關限制）。忘了 PIN 的話請改按「重設」；他這堂課已經 PIN 錯太多次被鎖的話，請在點名頁用「手動簽到」補登。" in t


# 說明頁對照表與空括號

def test_help_table_has_new_rows(client):
    t = client.get("/help").text
    for first in ("這個學號這一場已經有紀錄（出席／遲到…）", "PIN 錯太多次，本次請找老師手動簽到", "老師今天叫名時這個學號不在場",
                  "這份簽到表單和這支手機對不上", "送太多次了", "點名已經結束"):
        assert f"<td>{first}</td>" in t, first
    assert "兩種情況：你的手機曾用來簽過別人的學號，或你填的姓名和名冊寫法不完全一樣。老師會當場確認，確認前不算出席。" in t
    assert "老師關了首次綁定。第一次簽的人、或老師今天之前重設過你 PIN 的人，請老師在點名頁打開，再重新掃 QR。" in t
    assert "出席與遲到時間都過了，或老師按了結束。有到課的直接找老師；老師如果說取消結束了，重新掃 QR。" in t
    assert t.index("PIN 錯誤，剩 N 次") < t.index("PIN 錯太多次，本次請找老師手動簽到") < t.index("瀏覽器沒有保存簽到識別")


def test_manual_present_banner_has_no_empty_parentheses(h):
    s0 = h.open("CD")
    assert h.checkin(s0, "dev15-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s0}/close")
    s1 = h.open("CD")
    assert h.T.post(f"/api/t/session/{s1}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"}).status_code == 200
    code = h.T.get(f"/api/t/session/{s1}/qr").json()["code"]
    html = h.dev("dev15-A").post("/c", data={"code": code}).text
    assert "alreadyBanner" in html and "（）" not in html, html[-2500:]


# 文案

def test_copy_changes(h):
    from pathlib import Path
    root = Path(h.A.__file__).parent / "templates"
    sess = (root / "session.html").read_text(encoding="utf-8")
    assert "'結束這一場綁定？（綁定場次不記缺席）'" in sess
    assert "'\\n出席窗還要 '+Math.max(1,Math.ceil((state.open_until-state.now)/60))+' 分鐘才關。'" in sess
    assert "出席窗還沒到，還剩" not in sess
    assert "不在場：已撤回這個學號的綁定，今天不能再綁；人其實在場的話，下次上課再綁（首次綁定要開著）" in sess
    assert "按錯也救不回來，只能下次上課再綁（首次綁定要開著）。" in sess
    stu = (root / "students.html").read_text(encoding="utf-8")
    assert "今天不能再綁；人在教室的話，今天的出缺勤請在點名頁改，下次上課再綁（首次綁定要開著）。" in stu
    assert "誤按結束的話，10 分鐘內在點名頁按「取消結束」。出席窗與遲到窗都過了，或已經按過結束。" in (root / "home.html").read_text(encoding="utf-8")


# 結案清 PIN 去重記憶體

def test_reopen_restarts_pin_fail_count_not_stale_dedup(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        s0 = h.open("CD")
        assert h.checkin(s0, "dev16-A", "9994A001", name="學生1", pin="1234")["ok"]
        h.T.post(f"/api/t/session/{s0}/close")
        tr.shift(5)
        sid = h.open("CD")
        cl = h.dev("dev16-new")
        for p in ("1111", "2222", "3333"):
            tr.shift(1)
            r = cl.post("/api/checkin", data={"grant": h.grant(sid, cl), "student_id": "9994A001", "name": "", "pin": p, "pin2": "", "hints": "h"})
            assert r.status_code == 401, r.text
        tr.shift(5)
        h.T.post(f"/api/t/session/{sid}/close")
        tr.shift(5)
        assert _reopen(h, sid).status_code == 200
        tr.shift(5)
        r = cl.post("/api/checkin", data={"grant": h.grant(sid, cl), "student_id": "9994A001", "name": "", "pin": "3333", "pin2": "", "hints": "h"})
        assert r.status_code == 401 and r.json()["error"] == "PIN 錯誤，剩 2 次。先確認學號沒打錯；忘記了請找老師重設", r.text


# 新環境落地（落地事件沒有 student_pk）、之後被擋的事件才帶 student_pk，老師手動記了之後不再算「在填表」

def _reset_hint_setup(h, tag):
    """甲（999000001 王測甲）用家裡那支綁好、結束；新的一場，另有一支（乙裝置）落地在填。回 (sid, 新環境名)。"""
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (h.cid["CD"], "999000001", "王測甲", ""))
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-home", "999000001", name="王測甲", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    sid = h.open("CD")
    _land_how(h, sid, f"{tag}-filler", "qr")
    _land_how(h, sid, f"{tag}-new", "qr")
    return sid, f"{tag}-new"


def _reset_hint_post(h, sid, dev, name, pin):
    cl = h.dev(dev)
    return cl.post("/api/checkin", data={"grant": h.grant(sid, cl), "student_id": "999000001", "name": name, "pin": pin,
                                         "pin2": "", "hints": "h"})


def test_1_blocked_new_env_not_counted_after_teacher_records(h):
    sid, new = _reset_hint_setup(h, "reset-hint-a")
    assert h.state(sid)["filling"] == 2
    for _ in range(2):
        assert _reset_hint_post(h, sid, new, "王測甲", "9999").status_code == 401
    assert h.state(sid)["filling"] == 2  # 老師還沒記
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "999000001"), "status": "present"}).status_code == 200
    assert h.state(sid)["filling"] == 1, h.state(sid)["filling"]


def test_2_blocked_new_env_still_counted_if_teacher_has_not_recorded(h):
    sid, new = _reset_hint_setup(h, "reset-hint-b")
    assert _reset_hint_post(h, sid, new, "王測甲", "9999").status_code == 401
    with h.A.db() as con:
        assert con.execute("SELECT student_pk FROM events WHERE session_id=? AND kind='checkin_reject'", (sid,)).fetchone()[0] is not None
    assert h.state(sid)["filling"] == 2, h.state(sid)["filling"]


def test_3_reject_without_student_pk_still_counted(h):
    sid, new = _reset_hint_setup(h, "reset-hint-c")
    r = _reset_hint_post(h, sid, new, "李完全不同", "9999")
    assert r.status_code == 400, r.text
    with h.A.db() as con:
        assert con.execute("SELECT student_pk FROM events WHERE session_id=? AND kind='checkin_reject'", (sid,)).fetchone()[0] is None
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "999000001"), "status": "present"}).status_code == 200
    assert h.state(sid)["filling"] == 2, h.state(sid)["filling"]


# 突變測試存活補測
# 每條測試用自己獨有的裝置名（以測試項目與角色命名）：
# Helpers._devs 是類別層快取，突變模式下 id(A) 每個測試都一樣，同名會拿到前一個測試的 client。
# 旗標一律 json.loads 後比 list（含個數），不用子字串。時間用整數時鐘 time_machine.travel(int(h.A.now()), tick=False)，不 sleep。

import json
import re
import time_machine


def _boundary_post(h, sid, dev, stu, name="", pin="1234", pin2=None, grant=None, ip="140.131.1.1", **extra):
    """直接 POST /api/checkin，回 Response（要看狀態碼、Set-Cookie 時用；UA 固定 iPhone → iOS-INAPP）。"""
    cl = h.dev(dev)
    data = {"grant": grant or h.grant(sid, cl), "student_id": stu, "name": name, "pin": pin,
            "pin2": pin if pin2 is None else pin2, "hints": "h", **extra}
    return cl.post("/api/checkin", data=data, headers={"user-agent": "iPhone", "cf-connecting-ip": ip})


def _dk(h, dev):
    return h.dev(dev).cookies.get(h.A.DK_COOKIE)


def _boundary_dev_hash(h, dev):
    return h.A.hash_device(_dk(h, dev))


def _boundary_events(h, sid, kind):
    with h.A.db() as con:
        return [dict(r) for r in con.execute(
            "SELECT reason, device8, ua_class, student_pk FROM events WHERE session_id=? AND kind=? ORDER BY id", (sid, kind))]


def _boundary_flags(h, sid, stu):
    return json.loads(h.db_rows(sid)[stu]["flags"])


def _boundary_close(h, sid):
    assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]


def _boundary_land(h, sid, cl):
    """走 /s/... 掃 QR 落地（記 land_ok），回 grant。"""
    r = cl.get(_student_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200, r.status_code
    return re.search(r'GRANT="([^"]+)"', r.text).group(1)


def _reject_is(h, sid, r, code, reason):
    assert r.status_code == code, (r.status_code, r.text)
    assert _last_reject(h, sid)["reason"] == reason, _last_reject(h, sid)


# T1 收據欄位

def test_1_receipt_fields_first_and_again(h):
    sid = h.open("CD")
    r = _boundary_post(h, sid, "dev17-1-p", "9994A001", name="學生1", pin="1234", seat="  A1 ")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == "學生1" and j["student_id"] == "9994A001" and j["seat"] == "A1", j
    assert j["kind"] == "normal" and j["again"] is False and j["status"] == "出席", j
    r2 = _boundary_post(h, sid, "dev17-1-p", "9994A001", name="學生1", pin="1234", seat="  A1 ")  # 新 grant、同一支手機：again 路徑
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    assert j2["again"] is True and j2["name"] == "學生1" and j2["student_id"] == "9994A001", j2
    assert j2["seat"] == "A1" and j2["kind"] == "normal" and j2["status"] == "出席", j2


# T2 pin_unchanged 只在「已綁手機＋帶了 pin2」

def test_2_pin_unchanged_only_when_bound_phone_sends_pin2(h):
    s1 = h.open("CD")
    assert _boundary_post(h, s1, "dev17-2-p", "9994A002", name="學生2", pin="1234").json()["ok"]
    _boundary_close(h, s1)
    s2 = h.open("CD")
    r = _boundary_post(h, s2, "dev17-2-p", "9994A002", pin="", pin2="")
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert "pin_unchanged" not in r.json(), r.json()
    _boundary_close(h, s2)
    s3 = h.open("CD")
    r = _boundary_post(h, s3, "dev17-2-p", "9994A002", pin="1234", pin2="1234")
    assert r.status_code == 200, r.text
    assert r.json().get("pin_unchanged") is True, r.json()


# T3 「已經設過 PIN」剩 1 次的訊息

def test_3_pin_exists_message_with_one_attempt_left(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-3-a", "9994A003", name="學生3", pin="1234").json()["ok"]
    r1 = _boundary_post(h, sid, "dev17-3-b", "9994A003", pin="0001")
    assert r1.status_code == 401 and "剩 2 次" in r1.json()["error"], r1.text
    r2 = _boundary_post(h, sid, "dev17-3-b", "9994A003", pin="0002")
    assert r2.status_code == 401, r2.text
    assert "剩 1 次" in r2.json()["error"] and "錯太多次" not in r2.json()["error"], r2.json()
    assert "已經設過 PIN" in r2.json()["error"], r2.json()


# T4 PIN 長度邊界

def test_4_pin_length_boundary_first_bind(h):
    sid = h.open("CD")
    r = _boundary_post(h, sid, "dev17-4a-p", "9994A001", name="學生1", pin="123456")
    assert r.status_code == 200 and r.json()["ok"], r.text
    r = _boundary_post(h, sid, "dev17-4a-q", "9994A002", name="學生2", pin="1234567")
    _reject_is(h, sid, r, 400, "pin_format")


def test_4_pin_length_boundary_rebind(h):
    s1 = h.open("CD")
    assert _boundary_post(h, s1, "dev17-4b-p1", "9994A003", name="學生3", pin="654321").json()["ok"]
    _boundary_close(h, s1)
    s2 = h.open("CD")
    r = _boundary_post(h, s2, "dev17-4b-p2", "9994A003", pin="654321")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json().get("rebound") is True, r.json()
    r = _boundary_post(h, s2, "dev17-4b-p3", "9994A003", pin="6543210")
    _reject_is(h, s2, r, 400, "pin_format")


# T5 每條被拒路徑的事件原因碼

def test_5a_grant_issued_not_a_number(h):
    sid = h.open("CD")
    h.dev("dev17-5a-p")
    tok = h.A.make_token("g", str(sid), "n0", "abc", _boundary_dev_hash(h, "dev17-5a-p")[:16], exp=h.A.now() + 60)
    r = _boundary_post(h, sid, "dev17-5a-p", "9994A001", name="學生1", grant=tok)
    _reject_is(h, sid, r, 410, "grant_expired")
    # 同一筆事件的裝置與 UA 欄位（殺 1429 1435）
    ev = _boundary_events(h, sid, "checkin_reject")[-1]
    assert ev["ua_class"] == "iOS-INAPP", ev
    assert ev["device8"] == _boundary_dev_hash(h, "dev17-5a-p")[:8], ev


def test_5b_grant_issued_before_opened_at(h):
    sid = h.open("CD")
    h.dev("dev17-5b-p")
    opened = _sess(h, sid)["opened_at"]
    tok = h.A.make_token("g", str(sid), "n0", str(opened - 10), _boundary_dev_hash(h, "dev17-5b-p")[:16], exp=h.A.now() + 60)
    r = _boundary_post(h, sid, "dev17-5b-p", "9994A001", name="學生1", grant=tok)
    _reject_is(h, sid, r, 410, "grant_expired")


def test_5c_session_deleted_reject(h):
    sid = h.open("CD")
    cl = h.dev("dev17-5c-p")
    g = h.grant(sid, cl)
    assert h.T.post(f"/api/t/session/{sid}/delete").status_code == 200
    r = _boundary_post(h, sid, "dev17-5c-p", "9994A001", name="學生1", grant=g)
    _reject_is(h, sid, r, 410, "closed")
    assert "老師已取消" in r.json()["error"], r.json()


def test_5d_bind_session_opened_after_normal_closed(h):
    sid = h.open("CD")
    g = h.grant(sid, h.dev("dev17-5d-p"))
    _boundary_close(h, sid)
    h.open("CD", kind="bind")
    r = _boundary_post(h, sid, "dev17-5d-p", "9994A001", name="學生1", grant=g)
    _reject_is(h, sid, r, 410, "closed")
    assert r.json()["error"] == h.A.BIND_ONLY_CLOSED_MSG, r.json()


def test_5e_new_normal_session_opened_after_normal_closed(h):
    sid = h.open("CD")
    g = h.grant(sid, h.dev("dev17-5e-p"))
    _boundary_close(h, sid)
    h.open("CD")
    r = _boundary_post(h, sid, "dev17-5e-p", "9994A001", name="學生1", grant=g)
    _reject_is(h, sid, r, 410, "closed")
    assert "老師開了新的一場" in r.json()["error"], r.json()


def test_5f_pin_locked_reject(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-5f-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    for wrong in ("0001", "0002", "0003"):  # 每次不同的錯 PIN（避開 60 秒內同值去重），pin_max_fail 預設 3
        assert _boundary_post(h, sid, "dev17-5f-b", "9994A001", pin=wrong, pin2="").status_code == 401
    r = _boundary_post(h, sid, "dev17-5f-b", "9994A001", pin="1234", pin2="")
    _reject_is(h, sid, r, 423, "pin_locked")
    assert "PIN 錯太多次" in r.json()["error"], r.json()


def test_5g_first_bind_blank_name(h):
    sid = h.open("CD")
    r = _boundary_post(h, sid, "dev17-5g-p", "9994A001", name="", pin="1234")
    _reject_is(h, sid, r, 400, "not_match")


def test_5h_other_phone_pin_too_short(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-5h-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    r = _boundary_post(h, sid, "dev17-5h-b", "9994A001", pin="12", pin2="")
    _reject_is(h, sid, r, 400, "pin_format")


def test_5i_bind_session_already_recorded(h):
    sid = h.open("CD", kind="bind")
    assert _boundary_post(h, sid, "dev17-5i-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    r = _boundary_post(h, sid, "dev17-5i-b", "9994A001", pin="1234", pin2="")
    _reject_is(h, sid, r, 409, "already_recorded")
    assert "只做綁定" in r.json()["error"], r.json()


def test_5j_no_cookie_event_has_empty_device8(h):
    sid, grant = _gate_setup(h, "dev17-5j-p")
    r = h.student().post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1", "pin": "1234",
                                               "pin2": "1234", "hints": "h"}, headers={"user-agent": "iPhone"})
    _reject_is(h, sid, r, 400, "no_cookie")
    ev = _boundary_events(h, sid, "checkin_reject")[-1]
    assert ev["device8"] == "", ev


# T6 簽到成功事件的欄位

def _last_ok(h, sid, n_expected):
    evs = _boundary_events(h, sid, "checkin_ok")
    assert len(evs) == n_expected, evs
    return evs[-1]


def test_6ab_checkin_ok_event_first_and_again(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A001")
    assert _boundary_post(h, sid, "dev17-6ab-p", "9994A001", name="學生1").json()["ok"]
    dh8 = _boundary_dev_hash(h, "dev17-6ab-p")[:8]
    ev = _last_ok(h, sid, 1)
    assert ev == {"reason": "present", "device8": dh8, "ua_class": "iOS-INAPP", "student_pk": pk}, ev
    j = _boundary_post(h, sid, "dev17-6ab-p", "9994A001", name="學生1").json()
    assert j["again"] is True
    ev = _last_ok(h, sid, 2)
    assert ev == {"reason": "present", "device8": dh8, "ua_class": "iOS-INAPP", "student_pk": pk}, ev


def _set_cookie_values(r):
    return r.headers.get_list("set-cookie")


def test_6c_receipt_after_close_with_original_grant(h):
    sid = h.open("CD")
    cl = h.dev("dev17-6c-p")
    g = h.grant(sid, cl)
    pk = h.pk("CD", "9994A001")
    assert _boundary_post(h, sid, "dev17-6c-p", "9994A001", name="學生1", grant=g).json()["ok"]
    _last_ok(h, sid, 1)
    _boundary_close(h, sid)
    r = _boundary_post(h, sid, "dev17-6c-p", "9994A001", name="學生1", grant=g)
    assert r.status_code == 200 and r.json()["again"] is True, r.text
    ev = _last_ok(h, sid, 2)
    assert ev["reason"] in ("present", h.A.STATUS_LABEL["present"]), ev
    assert ev["device8"] == _boundary_dev_hash(h, "dev17-6c-p")[:8] and ev["ua_class"] == "iOS-INAPP" and ev["student_pk"] == pk, ev
    dk = _dk(h, "dev17-6c-p")
    assert any(v.startswith(f"{h.A.DK_COOKIE}={dk}") for v in _set_cookie_values(r)), _set_cookie_values(r)


def test_6d_receipt_after_grant_expired(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        cl = h.dev("dev17-6d-p")
        g = h.grant(sid, cl)
        pk = h.pk("CD", "9994A001")
        with h.A.db() as con:
            gs = h.A.course_settings(h.A.get_course(con, h.cid["CD"]))["grant_seconds"]
        assert _boundary_post(h, sid, "dev17-6d-p", "9994A001", name="學生1", grant=g).json()["ok"]
        _last_ok(h, sid, 1)
        tr.shift(gs + 5)
        r = _boundary_post(h, sid, "dev17-6d-p", "9994A001", name="學生1", grant=g)
        assert r.status_code == 200 and r.json()["again"] is True, r.text
        ev = _last_ok(h, sid, 2)
        assert ev["reason"] in ("present", h.A.STATUS_LABEL["present"]), ev
        assert ev["device8"] == _boundary_dev_hash(h, "dev17-6d-p")[:8] and ev["ua_class"] == "iOS-INAPP" and ev["student_pk"] == pk, ev
        dk = _dk(h, "dev17-6d-p")
        assert any(v.startswith(f"{h.A.DK_COOKIE}={dk}") for v in _set_cookie_values(r)), _set_cookie_values(r)


# T7 舊名 cookie 遷移走得到簽到端點

def test_7_legacy_cookie_migrates_through_checkin_endpoint(h, monkeypatch):
    s1 = h.open("CD")
    assert _boundary_post(h, s1, "dev17-7-p", "9994A001", name="學生1", pin="1234").json()["ok"]  # http 下正常綁好
    _boundary_close(h, s1)
    s2 = h.open("CD")
    cl = h.dev("dev17-7-p")
    val = _dk(h, "dev17-7-p")
    g_normal, g_again = h.grant(s2, cl), h.grant(s2, cl)  # patch 之前拿，dh16 是這支手機的
    monkeypatch.setattr(h.A, "DK_COOKIE", "__Host-attend_dk")

    def legacy_only():
        c = h.student()
        c.cookies.set("attend_dk", val)  # client 只帶舊名
        return c

    def post(c, grant):
        return c.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "", "pin": "", "pin2": "", "hints": "h"},
                      headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})

    r = post(legacy_only(), g_normal)  # 一般路徑
    assert r.status_code == 200 and r.json()["again"] is False, r.text
    assert any(v.startswith(f"__Host-attend_dk={val}") for v in _set_cookie_values(r)), _set_cookie_values(r)
    r = post(legacy_only(), g_again)  # again 路徑
    assert r.status_code == 200 and r.json()["again"] is True, r.text
    assert any(v.startswith(f"__Host-attend_dk={val}") for v in _set_cookie_values(r)), _set_cookie_values(r)


# T8 改綁事件欄位

def test_8_rebind_event_after_unbind_only(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A004")
    assert _boundary_post(h, sid, "dev17-8-a", "9994A004", name="學生4", pin="1234").json()["ok"]
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""}).json()["ok"]
    r = _boundary_post(h, sid, "dev17-8-b", "9994A004", pin="1234", pin2="")
    assert r.status_code == 200, r.text
    evs = _boundary_events(h, sid, "rebind")
    assert evs, "沒有 rebind 事件"
    assert evs[-1] == {"reason": "", "device8": _boundary_dev_hash(h, "dev17-8-b")[:8], "ua_class": "iOS-INAPP", "student_pk": pk}, evs[-1]


# T9 非預期例外事件的欄位與歸戶

def test_9_checkin_error_event_fields_and_attribution(h, monkeypatch):
    sid, grant = _gate_setup(h, "dev17-9-p")
    pk = h.pk("CD", "9994A001")
    dh8 = _boundary_dev_hash(h, "dev17-9-p")[:8]
    monkeypatch.setattr(h.A, "course_settings", lambda row: (_ for _ in ()).throw(RuntimeError("boom")))

    def boom(name, **extra):
        with pytest.raises(RuntimeError):
            _boundary_post(h, sid, "dev17-9-p", "9994A001", name=name, grant=grant, **extra)
        return _boundary_events(h, sid, "checkin_error")[-1]

    ev = boom("學生1")  # a. 姓名完全相同：歸戶，欄位齊全
    assert ev == {"reason": "RuntimeError", "device8": dh8, "ua_class": "iOS-INAPP", "student_pk": pk}, ev
    assert boom("王大明")["student_pk"] is None  # b. 別人的姓名不歸戶
    assert boom("", name_hint="王大明")["student_pk"] is None  # c. 提示對不上
    assert boom("", name_hint="學生1")["student_pk"] == pk  # d. 提示對上
    # e. 竄改過簽章的 grant：連同 read_token_ignore_expiry 也炸，仍不歸戶
    monkeypatch.setattr(h.A, "read_token_ignore_expiry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom2")))
    bad = grant[:-1] + ("0" if grant[-1] != "0" else "1")
    n_before = len(_boundary_events(h, sid, "checkin_error"))
    with pytest.raises(RuntimeError):
        _boundary_post(h, sid, "dev17-9-p", "9994A001", name="學生1", grant=bad)
    evs = _boundary_events(h, sid, "checkin_error")
    assert len(evs) == n_before + 1, evs
    assert evs[-1]["student_pk"] is None, evs[-1]


# T10 已簽到頁的內容

def _landing(h, sid, dev):
    r = h.dev(dev).get(_student_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200, r.status_code
    return r.text


def _banner(text):
    """已簽到 banner 的兩行：<p class="who"> 與緊接著的 <p class="muted sm">（說明）。
    整頁比對會被頁尾的 JS 字串撞到：student.html 330–331 行的表單 JS 本來就帶著兩句待確認說明的全文，
    所以那兩句的「含／不含」只能在 banner 的說明行上判。"""
    m = re.search(r'<p class="who">(.*?)</p>\s*<p class="muted sm">(.*?)</p>', text, re.S)
    assert m, "找不到已簽到 banner"
    return m.group(1), m.group(2)


def test_10a_already_checked_page_present(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-10a-p", "9994A001", name="學生1").json()["status"] == "出席"
    text = _landing(h, sid, "dev17-10a-p")
    ts = h.A.fmt(h.db_rows(sid)["9994A001"]["ts"])
    for s in ("這一場已經簽到了", "學生1", "9994A001", "出席", "有疑問下課找老師", ts):
        assert s in text, s
    who, note = _banner(text)
    assert "出席" in who and note.strip() == "有疑問下課找老師", (who, note)
    for s in ("系統看到這支手機", "你填的姓名和名冊的寫法"):
        assert s not in who and s not in note, s


def test_10b_already_checked_page_late(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(_sess(h, sid)["open_until"] - h.A.now() + 1)
        assert _boundary_post(h, sid, "dev17-10b-p", "9994A001", name="學生1").json()["status"] == "遲到"
        text = _landing(h, sid, "dev17-10b-p")
        assert "這一場已經簽到了" in text and "遲到" in _banner(text)[0], text


def test_10c_already_checked_page_name_variant(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-10c-p", "9994A001", name="學生9").json()["status"] == "待確認"
    text = _landing(h, sid, "dev17-10c-p")
    assert "待確認" in text
    who, note = _banner(text)
    assert "待確認" in who and "你填的姓名和名冊的寫法不完全一樣" in note, (who, note)
    assert "有疑問下課找老師" not in text


def test_10d_already_checked_page_phone_signed_other_student(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-10d-p", "9994A001", name="學生1", pin="1111").json()["status"] == "出席"
    j = _boundary_post(h, sid, "dev17-10d-p", "9994A002", name="學生2", pin="2222").json()  # 同一支手機簽第二個學號
    # 旗標同時有 device_bound_other 與 same_device，pending_reason_from_flags 依序取 same_device
    assert j["status"] == "待確認" and j["pending_reason"] == "same_device", j
    row = h.db_rows(sid)["9994A001"]  # 被借的那一列轉待確認、旗標 same_device
    assert row["status"] == "pending" and "same_device" in json.loads(row["flags"]), row
    text = _landing(h, sid, "dev17-10d-p")
    for s in ("這一場已經簽到了", "學生1", "9994A001", "待確認"):
        assert s in text, s
    who, note = _banner(text)
    assert "學生1" in who and "9994A001" in who and "待確認" in who and "學生2" not in who, who
    assert "系統看到這支手機簽過別的學號" in note, note
    assert "你填的姓名和名冊的寫法" not in note and "有疑問下課找老師" not in text


def test_10e_already_checked_page_bind_session(h):
    sid = h.open("CD", kind="bind")
    assert _boundary_post(h, sid, "dev17-10e-p", "9994A001", name="學生1").json()["ok"]
    text = _landing(h, sid, "dev17-10e-p")
    assert "這一場已經綁定了" in text
    who, note = _banner(text)
    assert "出席" not in who and "已綁定（這一場不算出缺勤）" in who, who
    # 「今天還沒簽到成功的，請找老師補登。」在 student.html 的 JS 裡也有，整頁比對恆真，所以要取出該區塊再比
    assert note.strip() == "今天還沒簽到成功的，請找老師補登。", note


# T11 卡住名單

def _block_twice(h, sid, dev, stu, name):
    """首綁表單 PIN 格式不對兩次（pin_format，學號歸戶）。"""
    for p in ("12", "13"):
        r = _boundary_post(h, sid, dev, stu, name=name, pin=p)
        assert r.status_code == 400, r.text


def _boundary_stuck(h, sid):
    return h.state(sid)["stuck"]


def _stuck_by_sid(h, sid):
    return {x["student_id"]: x for x in _boundary_stuck(h, sid)}


def test_11a_succeeded_students_are_not_stuck(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(10)
        _block_twice(h, sid, "dev17-11a-1", "9994A001", "學生1")
        _block_twice(h, sid, "dev17-11a-2", "9994A002", "學生2")
        assert set(_stuck_by_sid(h, sid)) == {"9994A001", "9994A002"}  # 還沒成功前都在名單上
        tr.shift(10)
        with h.A.db() as con:  # 推過出席窗：遲到
            con.execute("UPDATE sessions SET open_until=? WHERE id=?", (h.A.now() - 1, sid))
        assert _boundary_post(h, sid, "dev17-11a-1", "9994A001", name="學生1").json()["status"] == "遲到"
        assert _boundary_post(h, sid, "dev17-11a-2", "9994A002", name="學生9").json()["status"] == "待確認"  # 姓名變體
        assert _stuck_by_sid(h, sid) == {}, _boundary_stuck(h, sid)


def test_11b_skipped_first_student_does_not_hide_the_next(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(10)
        _block_twice(h, sid, "dev17-11b-1", "9994A001", "學生1")
        _block_twice(h, sid, "dev17-11b-2", "9994A002", "學生2")
        tr.shift(10)
        assert _boundary_post(h, sid, "dev17-11b-1", "9994A001", name="學生1").json()["status"] == "出席"
        stuck = _stuck_by_sid(h, sid)
        assert set(stuck) == {"9994A002"}, stuck
        e = stuck["9994A002"]
        assert e["pk"] == h.pk("CD", "9994A002") and e["name"] == "學生2" and e["conflict"] is False, e


def test_11c_grab_walk_does_not_stop_at_first_student(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(5)
        assert _boundary_post(h, sid, "dev17-11c-jia", "9994A001", name="學生1", pin="1111").json()["ok"]
        assert _boundary_post(h, sid, "dev17-11c-yi", "9994A003", name="學生3", pin="3333").json()["ok"]
        assert _boundary_post(h, sid, "dev17-11c-a4", "9994A004", name="學生4", pin="4444").json()["ok"]
        tr.shift(5)
        # 9994A004：本人簽到後，新手機打對 PIN，被 already_recorded 擋（灰字）
        _reject_is(h, sid, _boundary_post(h, sid, "dev17-11c-a4new", "9994A004", pin="4444", pin2=""), 409, "already_recorded")
        # 9994A002：本場首綁，之後別支手機打錯 PIN（紅字）
        tr.shift(5)
        assert _boundary_post(h, sid, "dev17-11c-bing", "9994A002", name="學生2", pin="2222").json()["ok"]
        tr.shift(5)
        _reject_is(h, sid, _boundary_post(h, sid, "dev17-11c-ding", "9994A002", pin="9999", pin2=""), 401, "pin_wrong")
        stuck = _stuck_by_sid(h, sid)
        assert set(stuck) == {"9994A004", "9994A002"}, stuck
        g = stuck["9994A004"]
        assert g["info"] is True and g["conflict"] is False, g
        assert g["pk"] == h.pk("CD", "9994A004") and g["name"] == "學生4", g
        red = stuck["9994A002"]
        assert red["conflict"] is True, red
        assert red["pk"] == h.pk("CD", "9994A002") and red["name"] == "學生2", red
        assert "本場剛綁定" in red["reason_label"], red


def test_11d_rebound_student_grabbed_is_labelled_rebind(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        s1 = h.open("CD")
        assert _boundary_post(h, s1, "dev17-11d-p1", "9994A001", name="學生1", pin="1234").json()["ok"]
        _boundary_close(h, s1)
        s2 = h.open("CD")
        tr.shift(5)
        j = _boundary_post(h, s2, "dev17-11d-p2", "9994A001", pin="1234", pin2="").json()  # 新瀏覽器打對 PIN 改綁
        assert j["ok"] and j.get("rebound") is True, j
        tr.shift(5)
        _reject_is(h, s2, _boundary_post(h, s2, "dev17-11d-p1", "9994A001", pin="9999", pin2=""), 401, "pin_wrong")  # 舊手機打錯
        e = _stuck_by_sid(h, s2)["9994A001"]
        assert e["conflict"] is True and "改綁" in e["reason_label"], e


def test_11e_manual_row_without_manual_status_event_does_not_stop_walk(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(5)
        with h.A.db() as con:  # 模擬舊資料：手動列、ts=0、沒有 manual_status 事件；rowid 比紅字那位小
            con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, source) VALUES(?,?,?,?,?,?,'manual')",
                        (sid, h.pk("CD", "9994A001"), 0, "", "present", json.dumps(["manual"])))
        assert _boundary_events(h, sid, "manual_status") == []
        assert _boundary_post(h, sid, "dev17-11e-bing", "9994A002", name="學生2", pin="2222").json()["ok"]
        tr.shift(5)
        _reject_is(h, sid, _boundary_post(h, sid, "dev17-11e-ding", "9994A002", pin="9999", pin2=""), 401, "pin_wrong")
        stuck = _stuck_by_sid(h, sid)
        assert "9994A002" in stuck and stuck["9994A002"]["conflict"] is True, stuck
        assert "9994A001" not in stuck, stuck


# T12 改綁前紀錄的旗標保留與去重

def test_12_qr_after_manual_kept_and_deduped_before_rebind(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A001")
    assert _boundary_post(h, sid, "dev17-12-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    assert "late_bind" in _boundary_flags(h, sid, "9994A001")
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "absent"}).status_code == 200
    assert _boundary_flags(h, sid, "9994A001") == ["late_bind", "manual"]
    r = _boundary_post(h, sid, "dev17-12-b", "9994A001", pin="1234", pin2="")
    assert r.status_code == 409, r.text
    assert _last_reject(h, sid)["reason"] == "already_recorded"
    assert _boundary_flags(h, sid, "9994A001") == ["late_bind", "manual", "qr_after_manual"]
    r = _boundary_post(h, sid, "dev17-12-c", "9994A001", pin="1234", pin2="")
    assert r.status_code == 409, r.text
    fl = _boundary_flags(h, sid, "9994A001")
    assert fl.count("qr_after_manual") == 1 and "late_bind" in fl and "manual" in fl, fl


# T13 again 路徑的旗標

def test_13a_plain_resend_adds_no_flags(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-13a-p", "9994A002", name="學生2").json()["ok"]
    before = _boundary_flags(h, sid, "9994A002")
    j = _boundary_post(h, sid, "dev17-13a-p", "9994A002", name="學生2").json()
    assert j["again"] is True
    assert _boundary_flags(h, sid, "9994A002") == before


def test_13b_qr_after_manual_added_once_on_own_phone(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-13b-p", "9994A003", name="學生3").json()["ok"]
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A003"), "status": "absent"}).status_code == 200
    for _ in range(2):
        j = _boundary_post(h, sid, "dev17-13b-p", "9994A003", name="學生3").json()
        assert j["again"] is True and j["status"] == "缺席", j
    fl = _boundary_flags(h, sid, "9994A003")
    assert fl.count("qr_after_manual") == 1, fl


def test_13c_borrowed_phone_with_manual_record_adds_each_flag_once(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-13c-jia", "9994A004", name="學生4", pin="4444").json()["ok"]
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"}).status_code == 200
    for _ in range(2):
        j = _boundary_post(h, sid, "dev17-13c-jia", "9994A001", name="學生1", pin="1111").json()
        assert j["again"] is True, j
    fl = _boundary_flags(h, sid, "9994A001")
    assert fl.count("device_bound_other") == 1 and fl.count("same_device") == 1, fl
    assert h.db_rows(sid)["9994A001"]["status"] == "present"


# T15 同一支手機三個人簽，第一列的 same_device 只標一次

def test_15_same_device_flag_on_first_row_added_once(h):
    sid = h.open("CD")
    for stu, name, pin in (("9994A001", "學生1", "1111"), ("9994A002", "學生2", "2222"), ("9994A003", "學生3", "3333")):
        assert _boundary_post(h, sid, "dev17-15-jia", stu, name=name, pin=pin).json()["ok"]
    fl = _boundary_flags(h, sid, "9994A001")
    assert fl.count("same_device") == 1, fl


# T16 只有 same_device 一個衝突旗標也要轉待確認

def test_16_only_same_device_still_pending(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev17-16-jia", "9994A001", name="學生1", pin="1111").json()["ok"]
    assert h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": ""}).json()["ok"]
    j = _boundary_post(h, sid, "dev17-16-jia", "9994A002", name="學生2", pin="2222").json()
    assert j["status"] == "待確認" and j["pending_reason"] == "same_device", j
    fl = _boundary_flags(h, sid, "9994A002")
    assert "same_device" in fl and "device_bound_other" not in fl, fl


# T17 出席窗邊界

def test_17_scan_exactly_at_open_until_counts_as_present(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        cl = h.dev("dev17-17-p")
        tr.shift(_sess(h, sid)["open_until"] - h.A.now())
        assert h.A.now() == _sess(h, sid)["open_until"]
        g = h.grant(sid, cl)  # 走 /c，沒有 land_ok
        tr.shift(1)
        j = h.checkin(sid, "dev17-17-p", "9994A001", name="學生1", grant=g)
        assert j["ok"] and j["status"] == "出席", j


# T18 借手機只有一種衝突時也不繼承出借者的落地時間

def test_18a_only_other_does_not_inherit_owner_landing(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        s1 = h.open("CD")
        assert _boundary_post(h, s1, "dev17-18a-X", "9994A001", name="學生1", pin="1111").json()["ok"]
        _boundary_close(h, s1)
        s2 = h.open("CD")
        tr.shift(60)
        clx = h.dev("dev17-18a-X")
        _boundary_land(h, s2, clx)  # 開場 1 分鐘，只落地不送
        tr.shift(300)
        g = _boundary_land(h, s2, clx)
        j = _boundary_post(h, s2, "dev17-18a-X", "9994A003", name="學生3", pin="3333", grant=g).json()
        assert j["ok"], j
        assert "was_late" in _boundary_flags(h, s2, "9994A003")


def test_18b_only_same_does_not_inherit_owner_landing(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(60)
        clx = h.dev("dev17-18b-X")
        g1 = _boundary_land(h, sid, clx)
        assert _boundary_post(h, sid, "dev17-18b-X", "9994A001", name="學生1", pin="1111", grant=g1).json()["status"] == "出席"
        assert h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": ""}).json()["ok"]
        tr.shift(300)
        g = _boundary_land(h, sid, clx)
        j = _boundary_post(h, sid, "dev17-18b-X", "9994A003", name="學生3", pin="3333", grant=g).json()
        assert j["ok"], j
        fl = _boundary_flags(h, sid, "9994A003")
        assert "was_late" in fl and "same_device" in fl and "device_bound_other" not in fl, fl


# T19 被拒訊息不能是空的

def test_19a_device_throttle_message(h):
    sid = h.open("CD")
    cl = h.dev("dev17-19a-p")
    g = h.grant(sid, cl)
    for _ in range(12):
        assert _boundary_post(h, sid, "dev17-19a-p", "9994A001", name="王大明", grant=g).status_code == 400
    r = _boundary_post(h, sid, "dev17-19a-p", "9994A001", name="王大明", grant=g)
    _reject_is(h, sid, r, 429, "throttle")
    assert "送太多次了" in r.json()["error"], r.json()


def test_19b_device_throttle_window(h):
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        cl = h.dev("dev17-19b-p")
        g = h.grant(sid, cl)
        for _ in range(12):
            assert _boundary_post(h, sid, "dev17-19b-p", "9994A001", name="王大明", grant=g).status_code == 400
        assert _boundary_post(h, sid, "dev17-19b-p", "9994A001", name="王大明", grant=g).status_code == 429
        tr.shift(60)
        assert _boundary_post(h, sid, "dev17-19b-p", "9994A001", name="王大明", grant=g).status_code != 429


def test_19c_ip_throttle_message_other_ip_and_window(h):
    NAT, OTHER = "140.9.9.9", "140.9.9.10"
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        for j in range(20):  # 20 支未綁手機、同一出口 IP，各 12 次＝240 次，全都不是 429
            dev = f"dev17-19c-{j}"
            g = h.grant(sid, h.dev(dev))
            for _ in range(12):
                r = _boundary_post(h, sid, dev, "9994A001", name="王大明", grant=g, ip=NAT)
                assert r.status_code != 429, (j, r.text)
        r = _boundary_post(h, sid, "dev17-19c-20", "9994A001", name="王大明", ip=NAT)  # 第 21 支的第 1 次
        _reject_is(h, sid, r, 429, "throttle")
        assert "送太多次了" in r.json()["error"], r.json()
        r = _boundary_post(h, sid, "dev17-19c-21", "9994A001", name="王大明", ip=OTHER)  # 另一個 IP 不受影響
        assert r.status_code != 429, r.text
        tr.shift(60)
        r = _boundary_post(h, sid, "dev17-19c-22", "9994A001", name="王大明", ip=NAT)  # 視窗過了
        assert r.status_code != 429, r.text


# 突變測試判讀為漏測、補上的斷言（do_checkin、compute_stuck）

def test_20a_first_bind_on_someone_elses_phone_writes_no_pin(h):
    # 手機已綁給別人（other）、本場還沒有別人用它簽（same 是空的）——首綁的人的 PIN 也不能寫進去（殺 655）
    s1 = h.open("CD")
    assert _boundary_post(h, s1, "dev17-20a-p", "9994A001", name="學生1", pin="1111").json()["ok"]
    _boundary_close(h, s1)
    s2 = h.open("CD")
    r = _boundary_post(h, s2, "dev17-20a-p", "9994A002", name="學生2", pin="2222")
    with h.A.db() as con:
        row = con.execute("SELECT pin_hash, device_hash FROM students WHERE id=?", (h.pk("CD", "9994A002"),)).fetchone()
    assert row["pin_hash"] is None and row["device_hash"] is None, (r.status_code, r.text)


def test_20b_grey_row_early_in_roster_does_not_stop_walk(h):
    # 灰字那筆排在名冊前面（9994A002），紅字那筆在後面（9994A004）：灰字之後要繼續走（殺 compute_stuck 244）
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(5)
        assert _boundary_post(h, sid, "dev17-20b-a2", "9994A002", name="學生2", pin="2222").json()["ok"]
        tr.shift(5)
        _reject_is(h, sid, _boundary_post(h, sid, "dev17-20b-a2new", "9994A002", pin="2222", pin2=""), 409, "already_recorded")
        tr.shift(5)
        assert _boundary_post(h, sid, "dev17-20b-a4", "9994A004", name="學生4", pin="4444").json()["ok"]
        tr.shift(5)
        _reject_is(h, sid, _boundary_post(h, sid, "dev17-20b-ding", "9994A004", pin="9999", pin2=""), 401, "pin_wrong")
        stuck = _stuck_by_sid(h, sid)
        assert set(stuck) == {"9994A002", "9994A004"}, stuck
        assert stuck["9994A002"]["info"] is True and stuck["9994A002"]["conflict"] is False, stuck
        assert stuck["9994A004"]["conflict"] is True, stuck


def test_20c_legacy_cookie_migrates_on_receipt_resend_paths(h, monkeypatch):
    # 已簽過的人重送：場次結束後（殺 369）、表單過期後（殺 114）兩條回收據的路徑，也要補發新名 cookie
    with time_machine.travel(int(h.A.now()), tick=False) as tr:
        sid = h.open("CD")
        cl = h.dev("dev17-20c-p")
        g = h.grant(sid, cl)
        with h.A.db() as con:
            gs = h.A.course_settings(h.A.get_course(con, h.cid["CD"]))["grant_seconds"]
        assert _boundary_post(h, sid, "dev17-20c-p", "9994A001", name="學生1", grant=g).json()["ok"]  # http 下正常綁好、簽好
        val = _dk(h, "dev17-20c-p")
        _boundary_close(h, sid)
        monkeypatch.setattr(h.A, "DK_COOKIE", "__Host-attend_dk")

        def post():
            c = h.student()
            c.cookies.set("attend_dk", val)  # client 只帶舊名
            return c.post("/api/checkin", data={"grant": g, "student_id": "9994A001", "name": "", "pin": "", "pin2": "", "hints": "h"},
                          headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})

        r = post()  # 場次已結束 → 回收據
        assert r.status_code == 200 and r.json()["again"] is True, r.text
        assert any(v.startswith(f"__Host-attend_dk={val}") for v in _set_cookie_values(r)), _set_cookie_values(r)
        tr.shift(gs + 5)
        r = post()  # 表單過期、簽章仍有效 → 回收據
        assert r.status_code == 200 and r.json()["again"] is True, r.text
        assert any(v.startswith(f"__Host-attend_dk={val}") for v in _set_cookie_values(r)), _set_cookie_values(r)


def test_20d_pin_mismatch_message(h):
    sid = h.open("CD")
    r = _boundary_post(h, sid, "dev17-20d-p", "9994A001", name="學生1", pin="1234", pin2="1235")
    _reject_is(h, sid, r, 400, "pin_mismatch2")
    assert r.json()["error"] == "兩次 PIN 不一樣", r.json()  # 殺 644


# 登出不能不登入就觸發

def _epoch(A):
    return A.auth_epoch()


def test_anonymous_get_logout_does_not_touch_epoch(A, h):
    anon = h.student()
    e0 = _epoch(A)
    r = anon.get("/t/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/t/login"
    assert _epoch(A) == e0
    assert h.T.get(f"/api/t/session/{h.open('CD')}/qr").status_code == 200


def test_anonymous_post_logout_is_401_and_epoch_unchanged(A, h):
    anon = h.student()
    e0 = _epoch(A)
    assert anon.post("/t/logout", follow_redirects=False).status_code == 401
    # 跨站情境：別的網站的表單帶不到 cookie，等同匿名
    assert anon.post("/t/logout", headers={"origin": "http://evil.example"}, follow_redirects=False).status_code == 401
    assert _epoch(A) == e0
    sid = h.open("CD")
    assert h.T.get(f"/api/t/session/{sid}/qr").status_code == 200


def test_teacher_get_shows_button_without_logging_out(A, h):
    e0 = _epoch(A)
    r = h.T.get("/t/logout", follow_redirects=False)
    assert r.status_code == 200
    assert 'method="post"' in r.text and 'action="/t/logout"' in r.text and "登出所有裝置" in r.text
    assert _epoch(A) == e0
    assert "ta" not in "".join(r.headers.get_list("set-cookie"))
    assert h.T.get("/t", follow_redirects=False).status_code == 200


def test_teacher_post_logout_bumps_epoch_and_revokes_old_token(A, h):
    old_cookie = h.T.cookies.get("ta")
    e0 = int(_epoch(A))
    r = h.T.post("/t/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/t/login"
    assert int(_epoch(A)) == e0 + 1
    c2 = h.student()
    c2.cookies.set("ta", old_cookie)
    assert c2.get("/api/t/session/1/state").status_code == 401


# 鎖定訊息要說到「重設」與「重新掃 QR」

def _assert_reset_hint(err):
    assert "重設" in err and "重新掃 QR" in err, err
    assert "這一場請找老師手動簽到" in err, err


def test_locked_423_message_mentions_reset(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev18-a-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    for wrong in ("0001", "0002", "0003"):
        assert _boundary_post(h, sid, "dev18-a-b", "9994A001", pin=wrong, pin2="").status_code == 401
    r = _boundary_post(h, sid, "dev18-a-b", "9994A001", pin="1234", pin2="")
    assert r.status_code == 423, r.text
    _assert_reset_hint(r.json()["error"])


def test_pin_wrong_zero_left_message_mentions_reset(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev18-b-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    r = None
    for wrong in ("0001", "0002", "0003"):
        r = _boundary_post(h, sid, "dev18-b-b", "9994A001", pin=wrong, pin2="")
    assert r is not None and r.status_code == 401 and "錯太多次" in r.json()["error"], r.text
    _assert_reset_hint(r.json()["error"])


def test_pin_exists_zero_left_message_mentions_reset(h):
    sid = h.open("CD")
    assert _boundary_post(h, sid, "dev18-c-a", "9994A001", name="學生1", pin="1234").json()["ok"]
    r = None
    for wrong in ("0001", "0002", "0003"):
        r = _boundary_post(h, sid, "dev18-c-b", "9994A001", name="學生1", pin=wrong, pin2=wrong)
    assert r is not None and r.status_code == 401 and "已經設過 PIN" in r.json()["error"] and "錯太多次" in r.json()["error"], r.text
    assert r.json()["error"].startswith("這個學號已經設過 PIN，你輸入的不是原本那組，"), r.json()
    _assert_reset_hint(r.json()["error"])


def test_session_manual_section_mentions_reset(h):
    sid = h.open("CD")
    t = h.T.get(f"/t/session/{sid}").text
    assert "到名冊頁按「重設」，下次才不會又卡" in t


# 老師手動記與學生簽到、或連按兩下，不能 500

def _teacher_client(h):
    from fastapi.testclient import TestClient
    c = TestClient(h.A.app, base_url="http://test")  # raise_server_exceptions 預設 True：未處理的例外（IntegrityError）會在呼叫端拋出
    c.cookies.set("ta", h.T.cookies.get("ta"))
    return c


def _post_status(client, sid, pk, status, out):
    try:
        r = client.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": status})
        out.append(r.status_code)
    except Exception as e:  # 老師端的 500 在 TestClient 會變成例外
        out.append(type(e).__name__)


def _student_post(h, sid, dev, stu, name, out, grant):
    cl = h.dev(dev)
    try:
        r = cl.post("/api/checkin", data={"grant": grant, "student_id": stu, "name": name, "pin": "1234", "pin2": "1234", "hints": "h"},
                    headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})
        out.append(r.status_code)
    except Exception as e:
        out.append(type(e).__name__)


class _B240JsonHook:
    """在 api_set_status「讀完 checkins、還沒 INSERT」之間（json.dumps(["manual"]) 那一行）插入另一個請求，
    把競態排成確定的交錯：舊程式（讀與寫不在同一交易）一定 IntegrityError；新程式（BEGIN IMMEDIATE）
    另一個請求會被鎖擋住，等老師這邊 commit 之後才進去。"""

    def __init__(self, real, interleave):
        self._real, self._interleave, self.fired = real, interleave, False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def dumps(self, obj, *a, **kw):
        if not self.fired and obj == ["manual"]:  # 只有老師手動記會寫出這個旗標組合
            self.fired = True
            self._interleave()
        return self._real.dumps(obj, *a, **kw)


def test_student_checkin_between_teacher_read_and_insert_is_not_500(h, monkeypatch):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A001")
    cl = h.dev("dev21-s")
    grant = h.grant(sid, cl)
    s_out, t_out, threads = [], [], []

    def interleave():
        t = threading.Thread(target=_student_post, args=(h, sid, "dev21-s", "9994A001", "學生1", s_out, grant))
        t.start(); threads.append(t)
        t.join(1.0)  # 舊程式：學生在這 1 秒內寫完；新程式：學生被老師的寫入鎖擋住，這裡逾時、老師繼續

    monkeypatch.setattr(h.A, "json", _B240JsonHook(h.A.json, interleave))
    _post_status(_teacher_client(h), sid, pk, "present", t_out)
    for t in threads:
        t.join(15)
    assert threads, "前置：交錯點沒有被走到"
    assert t_out == [200], f"老師端不該 500：{t_out}"
    assert s_out and s_out[0] in (200, 401, 409), f"學生端不該 500：{s_out}"
    assert "IntegrityError" not in t_out + s_out
    with h.A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()[0]
    assert n == 1


def test_two_rapid_manual_posts_do_not_500(h, monkeypatch):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A002")
    out1, out2, threads = [], [], []

    def interleave():
        t = threading.Thread(target=_post_status, args=(_teacher_client(h), sid, pk, "present", out2))
        t.start(); threads.append(t)
        t.join(1.0)

    monkeypatch.setattr(h.A, "json", _B240JsonHook(h.A.json, interleave))
    _post_status(_teacher_client(h), sid, pk, "present", out1)
    for t in threads:
        t.join(15)
    assert out1 == [200] and out2 == [200], (out1, out2)
    with h.A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()[0]
    assert n == 1


def test_random_race_20_rounds_no_500_no_integrity_error(h):
    import random
    rnd = random.Random(240)
    with h.A.db() as con:
        for i in range(20):
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (h.cid["CD"], f"9994B{i:03d}", f"競態{i:02d}", "創科一甲"))
    sid = h.open("CD")
    bad = []
    for i in range(20):
        stu, name = f"9994B{i:03d}", f"競態{i:02d}"
        pk = h.pk("CD", stu)
        grant = h.grant(sid, h.dev(f"dev21-r{i}"))
        s_out, t_out = [], []
        ts = threading.Thread(target=_student_post, args=(h, sid, f"dev21-r{i}", stu, name, s_out, grant))
        tt = threading.Thread(target=_post_status, args=(_teacher_client(h), sid, pk, "present", t_out))
        first, second = (ts, tt) if rnd.random() < 0.5 else (tt, ts)
        first.start(); time.sleep(rnd.uniform(0, 0.05)); second.start()
        ts.join(20); tt.join(20)
        with h.A.db() as con:
            n = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()[0]
        if t_out != [200] or not s_out or s_out[0] >= 500 or isinstance(s_out[0], str) or n != 1:
            bad.append((i, t_out, s_out, n))
    assert not bad, bad


# 手動簽到對「已經是這個狀態」的人不改任何東西

def _manual_status_events(h, sid, kind="manual_status"):
    with h.A.db() as con:
        return con.execute("SELECT * FROM events WHERE session_id=? AND kind=?", (sid, kind)).fetchall()


def test_unchanged_status_keeps_source_and_logs_nothing(h):
    sid = h.open("CD")
    assert h.checkin(sid, "dev20-a", "9994A001", name="學生1", pin="1234")["ok"]
    pk = h.pk("CD", "9994A001")
    before = h.db_rows(sid)["9994A001"]
    assert before["source"] == "qr" and before["status"] == "present"
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    assert r.status_code == 200 and r.json() == {"ok": True, "unchanged": True}, r.text
    after = h.db_rows(sid)["9994A001"]
    assert after == before, (before, after)  # source 仍是 qr、flags 沒多 manual、ts 沒動
    assert _manual_status_events(h, sid) == []


def test_pending_to_present_still_changes_and_adds_manual_flag(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A002")
    with h.A.db() as con:
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,'qr')",
                    (sid, pk, h.A.now(), "pending", "[]"))
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    assert r.status_code == 200 and "unchanged" not in r.json(), r.text
    row = h.db_rows(sid)["9994A002"]
    assert row["status"] == "present" and row["source"] == "manual" and "manual" in row["flags"]
    assert len(_manual_status_events(h, sid)) == 1
    # 再按一次同樣的狀態：這時才是 unchanged
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    assert r.json().get("unchanged") is True
    assert len(_manual_status_events(h, sid)) == 1


def test_new_row_and_other_status_unchanged_behaviour(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A003")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    assert r.json() == {"ok": True}, r.text
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "late"})
    assert r.json() == {"ok": True}, r.text
    assert h.db_rows(sid)["9994A003"]["status"] == "late"
    assert len(_manual_status_events(h, sid)) == 2


# 重設之後，卡住名單與「掃碼被擋」不再算重設之前的拒絕

def _stuck_hint_entry(h, sid, stu):
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        return [x for x in h.A.compute_stuck(con, sid, s["opened_at"]) if x["student_id"] == stu]


def _stuck_hint_setup(h, tag):
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-a", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    return h.open("CD")


def test_reset_clears_stuck_and_tried_until_new_rejects(h):
    sid = _stuck_hint_setup(h, "stuck-hint")
    for wrong in ("0001", "0002", "0003"):
        assert _boundary_post(h, sid, "dev22-b", "9994A001", pin=wrong, pin2="").status_code == 401
    assert len(_stuck_hint_entry(h, sid, "9994A001")) == 1
    assert h.row(sid, "9994A001")["tried"] != ""
    pk = h.pk("CD", "9994A001")
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"}).json()["ok"]
    assert _stuck_hint_entry(h, sid, "9994A001") == [], "重設之後立刻不在卡住名單"
    assert h.row(sid, "9994A001")["tried"] == "", "重設之後 tried 為空"
    # 重設後再被擋 2 次又會出現：重設把 PIN 清了，用姓名空白的首綁送出 → not_match，連兩次
    for _ in range(2):
        assert _boundary_post(h, sid, "dev22-c", "9994A001", name="", pin="1357").status_code == 400
    assert len(_stuck_hint_entry(h, sid, "9994A001")) == 1
    assert h.row(sid, "9994A001")["tried"] != ""


def test_plain_unbind_also_resets_floor(h):
    sid = _stuck_hint_setup(h, "stuck-hint-u")
    for wrong in ("0001", "0002"):
        assert _boundary_post(h, sid, "dev22u-b", "9994A001", pin=wrong, pin2="").status_code == 401
    assert len(_stuck_hint_entry(h, sid, "9994A001")) == 1
    pk = h.pk("CD", "9994A001")
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={}).json()["ok"]
    assert _stuck_hint_entry(h, sid, "9994A001") == []
    assert h.row(sid, "9994A001")["tried"] == ""


# 過期網址的落地事件要分得出「重訪」「舊場次」「真的過期」

def _kinds(h, sid):
    with h.A.db() as con:
        return [r["kind"] for r in con.execute(
            "SELECT kind FROM events WHERE session_id=? AND kind LIKE 'land_%' ORDER BY id", (sid,))]


def _funnel(h, sid):
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        return h.A.compute_funnel(con, sid, s["opened_at"])["total"]


_BAD_URL = "/s/{sid}/999999/deadbeef00"  # 簽章與時段都不對＝過期網址（同 test_teacher_friction 的寫法）


def test_signed_in_phone_revisiting_expired_url_logs_land_revisit(h):
    sid = h.open("CD")
    assert h.checkin(sid, "dev28-a", "9994A001", name="學生1", pin="1234")["ok"]
    r = h.dev("dev28-a").get(_BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200, r.status_code  # 本場已簽到：回 200 收據
    assert _kinds(h, sid) == ["land_revisit"], _kinds(h, sid)
    f = _funnel(h, sid)
    assert f["land_expired"] == 0, f


def test_old_session_url_after_close_logs_land_closed(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.student().get(_BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert _kinds(h, sid) == ["land_closed"], _kinds(h, sid)
    assert _funnel(h, sid)["land_expired"] == 0


def test_old_session_url_when_newer_session_exists_logs_land_closed(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev28-n", "9994A002", name="學生2", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    h.open("CD")
    # 已簽到的手機走舊場次網址：回 410 舊場次頁，不算過期
    r = h.dev("dev28-n").get(_BAD_URL.format(sid=s1), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert _kinds(h, s1) == ["land_closed"], _kinds(h, s1)


def test_really_expired_while_session_live_logs_land_expired(h):
    sid = h.open("CD")
    r = h.student().get(_BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert _kinds(h, sid) == ["land_expired"], _kinds(h, sid)
    assert _funnel(h, sid)["land_expired"] == 1


# 刪除此次後同號重開，舊場次的記憶體狀態不能帶進來

def _prior_and_open(h, tag):
    s0 = h.open("CD")
    assert h.checkin(s0, f"{tag}-a", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s0}/close")
    return h.open("CD")


def test_pin_dedup_memory_not_carried_into_reopened_same_id_session(h):
    s1 = _prior_and_open(h, "prior-open")
    for wrong in ("0001", "0002", "0003"):
        r = _boundary_post(h, s1, "dev24-b", "9994A001", pin=wrong, pin2="")
    assert "錯太多次" in r.json()["error"]
    assert h.T.post(f"/api/t/session/{s1}/delete").json()["ok"]
    s2 = h.open("CD")
    assert s2 == s1, "前置：刪除後重開沿用同一個場次號"
    r = _boundary_post(h, s2, "dev24-b", "9994A001", pin="0003", pin2="")  # 原樣重送 60 秒內同一組錯 PIN
    assert r.status_code == 401, r.text
    assert "剩 2 次" in r.json()["error"] and "錯太多次" not in r.json()["error"], r.json()


def test_projector_seen_and_notify_state_cleared_on_delete(h):
    s1 = _prior_and_open(h, "prior-open-p")
    code = h.T.get(f"/api/t/session/{s1}/qr").json()["code"]
    assert h.T.get(f"/api/t/session/{s1}/qr", params={"have": code}).status_code == 200
    assert h.A.PROJECTOR_SEEN.get(s1) is not None, "前置：投影頁取碼有記時間"
    h.A._notify_state[s1] = {"sending": False, "sent_at": h.A.now(), "digest": "x"}
    assert h.T.post(f"/api/t/session/{s1}/delete").json()["ok"]
    assert s1 not in h.A.PROJECTOR_SEEN and s1 not in h.A._notify_state
    s2 = h.open("CD")
    assert s2 == s1
    st = h.state(s2)
    assert st["projector_age"] is None, st["projector_age"]
    with h.A.db() as con:
        assert h.T.get(f"/api/t/session/{s2}/qr").json()["opened_at"] == h.A.get_session(con, s2)["opened_at"]


def test_qr_opened_at_differs_after_reopen(h):
    s1 = h.open("CD")
    o1 = h.T.get(f"/api/t/session/{s1}/qr").json()["opened_at"]
    h.T.post(f"/api/t/session/{s1}/delete")
    s2 = h.open("CD")
    assert s2 == s1
    assert h.T.get(f"/api/t/session/{s2}/qr").json()["opened_at"] != o1


# 綁定場次自己到期 → 取消結束 → 延長，不能當場結束

def test_bind_session_auto_expired_reopen_then_extend(h):
    import math
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD", kind="bind")
        s0 = _sess(h, sid)
        assert s0["open_until"] == s0["late_until"]
        tr.move_to(s0["late_until"] + 240)       # 到期 4 分鐘後老師才看點名頁
        assert h.state(sid)["phase"] == "closed"  # 這一輪輪詢觸發自動結案（closed_at＝late_until）
        assert _reopen(h, sid).status_code == 200
        # 取消結束後：可簽狀態，不是遲到區；投影頁（qr 回應）算出的剩餘分鐘 > 0
        st = h.state(sid)
        assert st["phase"] == "open", st["phase"]
        q = h.T.get(f"/api/t/session/{sid}/qr").json()
        assert q["phase"] == "open" and math.ceil((q["open_until"] - q["now"]) / 60) > 0, q
        s1 = _sess(h, sid)
        assert s1["open_until"] == s1["late_until"] > h.A.now(), s1  # 綁定場次沒有遲到區，兩個時間一樣
        # 取消結束後才寫入的簽到列
        tr.shift(10)
        assert h.checkin(sid, "dev31-a", "9994A002", name="學生2", pin="1234")["ok"]
        tr.shift(5)
        # 延長：late_until 一定比現在晚、也比延長前晚；場次仍可簽
        r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "late"})
        assert r.status_code == 200 and r.json()["ok"], r.text
        s2 = _sess(h, sid)
        assert s2["late_until"] > h.A.now() and s2["late_until"] > s1["late_until"], (s1, s2, h.A.now())
        assert s2["open_until"] == s2["late_until"], s2
        assert h.state(sid)["phase"] == "open"
        # 之後輪詢不會把它結案；到期自然結案後，closed_at 不早於任何簽到列
        h.state(sid)
        assert _sess(h, sid)["closed_at"] is None
        tr.move_to(s2["late_until"] + 5)
        h.state(sid)
        s3 = _sess(h, sid)
        assert s3["closed_at"] is not None
        with h.A.db() as con:
            ts = con.execute("SELECT MAX(ts) FROM checkins WHERE session_id=? AND ts>0", (sid,)).fetchone()[0]
        assert ts <= s3["closed_at"], (ts, s3["closed_at"])


def test_extend_bind_session_not_yet_expired_counts_from_late_until(h):
    """對照：還沒到期的綁定場次，延長照舊從 late_until 起算（max(late_until, now) 在這裡就是 late_until）。"""
    import time_machine
    with time_machine.travel(h.A.now(), tick=False):
        sid = h.open("CD", kind="bind")
        with h.A.db() as con:
            con.execute("UPDATE sessions SET open_until=?, late_until=? WHERE id=?",
                        (h.A.now() + 30, h.A.now() + 30, sid))
        # 場次還沒到期（late_until 在未來），延長從 late_until 起算
        r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "late"})
        assert r.status_code == 200
        s = _sess(h, sid)
        assert abs(s["late_until"] - (h.A.now() + 30 + 120)) < 1 and s["open_until"] == s["late_until"], s


# 自動結案不能拿交易外讀到的舊場次資料做判斷

class _B266Con:
    """包住連線：遇到自動結案的 `UPDATE sessions SET closed_at…` 之前，先跑一次 hook（只一次）。
    這就是「finalize_expired 已經在交易外讀到『遲到窗過了』、還沒拿到寫入鎖」的那個縫隙；
    hook 裡由另一條連線（老師的延長／暫緩請求）寫入並 commit，等於別人先拿到寫入鎖。"""

    def __init__(self, con, slot):
        self._c, self._slot = con, slot

    def execute(self, sql, *a):
        if self._slot[0] is not None and sql.lstrip().startswith("UPDATE sessions SET closed_at"):
            hook, self._slot[0] = self._slot[0], None
            hook()
        return self._c.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _discord_round_setup(h, monkeypatch, discord=True):
    """時鐘可控；課程設好 Discord 頻道、4 人名冊簽到 3 人（出席率夠高，不會因為出席率低而自動暫緩）。"""
    import json as _json
    clock = [h.A.now()]
    monkeypatch.setattr(h.A, "now", lambda: clock[0])
    posts = []
    monkeypatch.setattr(h.A, "_bg_discord_post", lambda sid, ch, tx: posts.append(sid))
    if discord:
        with h.A.db() as con:
            con.execute("UPDATE courses SET settings=? WHERE id=?", (_json.dumps({"discord_channel": "12345678901234567"}), h.cid["CD"]))
    slot = [None]
    real_connect = h.A.connect
    monkeypatch.setattr(h.A, "connect", lambda *a, **kw: _B266Con(real_connect(*a, **kw), slot))
    return clock, posts, slot


def _round(h, clock, slot, hook_request):
    """開一場、讓它走到遲到窗最後；自動結案在 UPDATE 前插入 hook_request（在遲到窗結束前 0.1 秒送出）。"""
    base = clock[0]
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET open_until=?, late_until=? WHERE id=?", (base + 60, base + 100, sid))
        for stu in ("9994A001", "9994A002", "9994A003"):
            pk = con.execute("SELECT id FROM students WHERE course_id=? AND student_id=?", (h.cid["CD"], stu)).fetchone()["id"]
            con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,'qr')",
                        (sid, pk, base + 5, "present", "[]"))
    out = []

    def hook():
        clock[0] = base + 99.9  # 老師按下去的那一刻，遲到窗還沒到
        out.append(hook_request(sid))
        clock[0] = base + 100.5

    clock[0] = base + 100.5  # 輪詢（finalize_expired）看到的是遲到窗已過
    slot[0] = hook
    h.state(sid)
    return sid, out[0], base


def _session(h, sid):
    with h.A.db() as con:
        return dict(h.A.get_session(con, sid))


def test_extend_that_returned_ok_is_not_followed_by_auto_close(h, monkeypatch):
    clock, posts, slot = _discord_round_setup(h, monkeypatch)
    tc = _teacher_client(h)
    bad = []
    for i in range(16):
        sid, r, base = _round(h, clock, slot, lambda sid: tc.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "late"}))
        s = _session(h, sid)
        if r.status_code == 200 and r.json().get("ok"):
            if s["closed_at"] is not None or s["late_until"] < base + 100 + 120 - 1:
                bad.append((i, "回 ok 卻被結案或沒延長", r.status_code, s["closed_at"], s["late_until"] - base))
            if sid in posts:
                bad.append((i, "回 ok 卻發出 Discord"))
        # 下一輪從遲到窗之後重新開始
        clock[0] = max(clock[0], s["late_until"]) + 1000
        with h.A.db() as con:
            con.execute("UPDATE sessions SET closed_at=COALESCE(closed_at, ?) WHERE id=?", (clock[0], sid))
    assert not bad, bad


def test_hold_that_returned_held_true_is_not_followed_by_discord_post(h, monkeypatch):
    clock, posts, slot = _discord_round_setup(h, monkeypatch)
    tc = _teacher_client(h)
    bad = []
    for i in range(8):
        sid, r, base = _round(h, clock, slot, lambda sid: tc.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}))
        s = _session(h, sid)
        if r.status_code == 200 and r.json().get("held") is True:
            if sid in posts:
                bad.append((i, "回 held:true 卻發出 Discord", s["note"]))
            if s["note"] != "discord_held_manual":
                bad.append((i, "note 被蓋掉", s["note"]))
        clock[0] += 1000
    assert not bad, bad
    assert not posts, posts


def test_untouched_expired_session_still_auto_closes_and_posts(h, monkeypatch):
    """對照：沒人插手時，遲到窗過了照常自動結案、照常發（修正不能把自動結案本身弄壞）。"""
    clock, posts, slot = _discord_round_setup(h, monkeypatch)
    sid, _, base = _round(h, clock, slot, lambda sid: type("R", (), {"status_code": 0, "json": staticmethod(lambda: {})})())
    s = _session(h, sid)
    assert s["closed_at"] == base + 100, s["closed_at"]
    assert posts == [sid], posts


# 結束點名後的提示文字不指位置
# 上面 test_4 逐字比對的是「點名頁的「手動簽到」」，不寫「最下方」這類位置。

def _wording_close(h, sid):
    assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"]


# 尾巴：「最下方『手動簽到』」字句過時

def test_tail_manual_section_wording_has_no_position(h):
    assert "最下方" not in h.A.EXTEND_CLOSED_MSG and "點名頁的「手動簽到」" in h.A.EXTEND_CLOSED_MSG, h.A.EXTEND_CLOSED_MSG
    sid = h.open("CD")
    _wording_close(h, sid)
    r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2})
    assert r.status_code == 409 and "點名頁的「手動簽到」" in r.json()["error"] and "最下方" not in r.json()["error"], r.text


# 忘記 PIN、換了瀏覽器

def _whoami_setup(h, tag="whoami"):
    """A001／A002 各自在自己的手機綁定並設 PIN（1234／2222），回傳進行中的新場次。"""
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-own1", "9994A001", name="學生1", pin="1234", ua="UA-1")["ok"]
    assert h.checkin(s1, f"{tag}-own2", "9994A002", name="學生2", pin="2222", ua="UA-2")["ok"]
    _wording_close(h, s1)
    return h.open("CD")


def _whoami(h, dev, course="CD"):
    return h.dev(dev).post("/api/whoami", data={"course_id": h.cid[course]}).json()


def test_whoami_pin_only_for_same_device_that_failed_pin(h):
    sid = _whoami_setup(h)
    assert _whoami(h, "dev19-new") == {"known": False}  # 還沒試過：照舊
    # 首綁表單替已設過 PIN 的學號設新 PIN → pin_exists（寫了一筆 pin_fail）
    assert _boundary_post(h, sid, "dev19-new", "9994A001", name="學生1", pin="9999").status_code == 401
    j = _whoami(h, "dev19-new")
    assert j == {"known": False, "pin_only": {"student_id": "9994A001"}}, j
    # 別支裝置、沒有 cookie：照舊 known:false，不洩漏
    assert _whoami(h, "dev19-other") == {"known": False}
    anon = h.student()
    assert anon.post("/api/whoami", data={"course_id": h.cid["CD"]}).json() == {"known": False}
    # 別門課沒有進行中的場次：沒有
    assert _whoami(h, "dev19-new", "DT") == {"known": False}


def test_whoami_locked_when_device_reached_pin_max_fail(h):
    sid = _whoami_setup(h, "whoami-l")
    assert _boundary_post(h, sid, "dev19l-new", "9994A001", name="學生1", pin="9999").status_code == 401
    assert "locked" not in _whoami(h, "dev19l-new")
    assert _boundary_post(h, sid, "dev19l-new", "9994A001", pin="0001", pin2="").status_code == 401
    assert _boundary_post(h, sid, "dev19l-new", "9994A001", pin="0002", pin2="").status_code == 401
    j = _whoami(h, "dev19l-new")
    assert j == {"known": False, "pin_only": {"student_id": "9994A001"}, "locked": True}, j


def test_whoami_pin_only_cleared_by_reset_and_prefers_latest_student(h):
    sid = _whoami_setup(h, "whoami-r")
    assert _boundary_post(h, sid, "dev19r-new", "9994A001", name="學生1", pin="9999").status_code == 401
    assert _boundary_post(h, sid, "dev19r-new", "9994A002", name="學生2", pin="9999").status_code == 401
    assert _whoami(h, "dev19r-new")["pin_only"] == {"student_id": "9994A002"}  # 多個學號取最近更新的
    assert _boundary_post(h, sid, "dev19r-new", "9994A001", pin="0001", pin2="").status_code == 401
    assert _whoami(h, "dev19r-new")["pin_only"] == {"student_id": "9994A001"}
    for stu in ("9994A001", "9994A002"):
        assert h.T.post(f"/api/t/student/{h.pk('CD', stu)}/unbind", data={"reset_pin": "1"}).json()["ok"]
    assert _whoami(h, "dev19r-new") == {"known": False}  # 重設清掉 pin_fail 之後沒有


def test_whoami_pin_only_not_given_to_bound_device(h):
    _whoami_setup(h, "whoami-b")
    j = _whoami(h, "whoami-b-own1")
    assert j["known"] is True and "pin_only" not in j, j


# 老師剛處理過的同學又試一次，紅字改灰字

def _prepare(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev23-own", "9994A001", name="學生1", pin="1234", ua="UA-1")["ok"]
    assert h.checkin(s1, "dev23-own2", "9994A002", name="學生2", pin="2222", ua="UA-2")["ok"]
    _wording_close(h, s1)
    return h.open("CD")


def _prepared_stuck(h, sid, stu):
    return [x for x in h.state(sid)["stuck"] if x["student_id"] == stu]


def test_after_manual_pin_wrong_is_grey_for_10_minutes_then_red(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = _prepare(h)
        pk = h.pk("CD", "9994A001")
        assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"}).json()["ok"]
        tr.shift(120)
        assert _boundary_post(h, sid, "dev23-try", "9994A001", pin="0001", pin2="").status_code == 401
        got = _prepared_stuck(h, sid, "9994A001")
        assert len(got) == 1 and got[0]["info"] is True and got[0]["conflict"] is False, got
        assert got[0]["reason_label"] == "剛處理過的同學又試了一次", got
        tr.shift(9 * 60)  # 手動記之後 11 分鐘
        assert _boundary_post(h, sid, "dev23-try", "9994A001", pin="0002", pin2="").status_code == 401
        got = _prepared_stuck(h, sid, "9994A001")
        assert len(got) == 1 and got[0]["conflict"] is True and not got[0].get("info"), got


def test_after_unbind_grey_and_unhandled_student_stays_red(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = _prepare(h)
        assert h.checkin(sid, "dev23-own", "9994A001", name="", pin="")["ok"]
        assert h.checkin(sid, "dev23-own2", "9994A002", name="", pin="")["ok"]
        assert h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={}).json()["ok"]
        tr.shift(120)
        for stu in ("9994A001", "9994A002"):
            assert _boundary_post(h, sid, "dev23-try", stu, pin="0001", pin2="").status_code == 401
        a1, a2 = _prepared_stuck(h, sid, "9994A001"), _prepared_stuck(h, sid, "9994A002")
        assert len(a1) == 1 and a1[0]["info"] is True and a1[0]["conflict"] is False, a1
        assert len(a2) == 1 and a2[0]["conflict"] is True and not a2[0].get("info"), a2  # 沒處理過的人：紅字照舊


def test_after_reset_pin_exists_is_grey(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = _prepare(h)
        assert h.checkin(sid, "dev23-own", "9994A001", name="", pin="")["ok"]
        assert h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": "1"}).json()["ok"]
        # 重設後他在另一支手機重新設 PIN 並綁定，本場已有簽到列所以只回收據
        assert _boundary_post(h, sid, "dev23-x", "9994A001", name="學生1", pin="5555").status_code == 200
        tr.shift(60)
        assert _boundary_post(h, sid, "dev23-y", "9994A001", name="學生1", pin="6666").status_code == 401  # pin_exists
        got = _prepared_stuck(h, sid, "9994A001")
        assert len(got) == 1 and got[0]["info"] is True and got[0]["conflict"] is False, got
        assert got[0]["reason_label"] == "剛處理過的同學又試了一次", got


# 私密分頁替人首綁或改綁，同指紋轉待確認

def _same_fp_setup(h):
    """A002（H）與 A001（V）各在自己的手機綁定並設 PIN；回傳進行中的一般場次。"""
    b = h.open("CD", kind="bind")
    assert h.checkin(b, "dev25-h", "9994A002", name="學生2", pin="2222", ua="UA-H", ip="1.1.1.1")["ok"]
    assert h.checkin(b, "dev25-v", "9994A001", name="學生1", pin="1111", ua="UA-V", ip="2.2.2.2")["ok"]
    _wording_close(h, b)
    return h.open("CD")


def test_rebind_with_same_fp_becomes_pending_with_flag(h):
    sid = _same_fp_setup(h)
    assert h.checkin(sid, "dev25-h", "9994A002", name="", pin="", ua="UX", ip="9.9.9.9")["ok"]
    j = h.checkin(sid, "dev25-tab", "9994A001", name="學生1", pin="1111", pin2="", ua="UX", ip="9.9.9.9")
    assert j["ok"] and j["status"] == "待確認" and j["pending_reason"] == "same_fp", j
    row = h.row(sid, "9994A001")
    assert row["status"] == "pending" and "rebind" in row["flags"] and "same_fp" in row["flags"], row
    assert h.row(sid, "9994A002")["status"] == "present"


def test_rebind_with_different_ua_or_ip_stays_present(h):
    sid = _same_fp_setup(h)
    assert h.checkin(sid, "dev25-h", "9994A002", name="", pin="", ua="UX", ip="9.9.9.9")["ok"]
    j = h.checkin(sid, "dev25-tab", "9994A001", name="學生1", pin="1111", pin2="", ua="UY", ip="9.9.9.9")  # 不同 UA
    assert j["status"] == "出席", j
    assert "same_fp" not in h.row(sid, "9994A001")["flags"]
    _wording_close(h, sid)
    sid2 = _same_fp_setup(h)
    assert h.checkin(sid2, "dev25-h", "9994A002", name="", pin="", ua="UX", ip="9.9.9.9")["ok"]
    j = h.checkin(sid2, "dev25-tab", "9994A001", name="學生1", pin="1111", pin2="", ua="UX", ip="8.8.8.8")  # 不同 IP
    assert j["status"] == "出席", j
    assert "same_fp" not in h.row(sid2, "9994A001")["flags"]


def test_bind_session_only_flags_without_changing_status(h):
    b0 = h.open("CD", kind="bind")
    assert h.checkin(b0, "dev25b-h", "9994A002", name="學生2", pin="2222", ua="UA-H", ip="1.1.1.1")["ok"]
    assert h.checkin(b0, "dev25b-v", "9994A001", name="學生1", pin="1111", ua="UA-V", ip="2.2.2.2")["ok"]
    _wording_close(h, b0)
    sid = h.open("CD", kind="bind")
    assert h.checkin(sid, "dev25b-h", "9994A002", name="", pin="", ua="UX", ip="9.9.9.9")["ok"]
    j = h.checkin(sid, "dev25b-tab", "9994A001", name="學生1", pin="1111", pin2="", ua="UX", ip="9.9.9.9")
    assert j["ok"] and j["status"] == "出席", j  # 綁定場次：只加旗標
    row = h.row(sid, "9994A001")
    assert row["status"] == "present" and "same_fp" in row["flags"] and "rebind" in row["flags"], row


def test_first_bind_with_same_fp_becomes_pending(h):
    sid = _same_fp_setup(h)
    assert h.checkin(sid, "dev25-h", "9994A002", name="", pin="", ua="UX", ip="9.9.9.9")["ok"]
    j = h.checkin(sid, "dev25-first", "9994A003", name="學生3", pin="3333", ua="UX", ip="9.9.9.9")  # 從沒綁過的人，私密分頁首綁
    assert j["ok"] and j["status"] == "待確認", j
    row = h.row(sid, "9994A003")
    assert row["status"] == "pending" and "same_fp" in row["flags"] and "late_bind" in row["flags"], row


def test_faq_matches_behaviour(h):
    html = h.T.get("/t").text
    rows = {m.group(1): m.group(2) for m in re.finditer(r"<tr><td>(名單標「[^<]*」)</td><td>(.*?)</td></tr>", html, re.S)}
    assert "待確認" in rows["名單標「疑似同手機」"] and "改綁" in rows["名單標「疑似同手機」"], rows["名單標「疑似同手機」"]
    burst = rows["名單標「同來源多筆首綁」"]
    assert "只是提示" in burst and "先停在待確認" not in burst, burst


# 「IP 不同」不再顯示（資料照存）

def test_ip_differs_still_stored_but_page_and_faq_do_not_show_it(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "1.1.1.1"})
    assert h.checkin(sid, "dev27-a", "9994A001", name="學生1", ip="9.9.9.9")["ok"]
    assert "ip_differs" in h.db_rows(sid)["9994A001"]["flags"]
    row = h.row(sid, "9994A001")
    assert row["ip"] == "9.9.9.9" and "ip_differs" in row["flags"], row  # 資料與 state 照舊
    page = h.T.get(f"/t/session/{sid}").text
    assert "r.ip&&state.teacher_ip" not in page and "ipMutedHint" not in page, "名單不再輸出學生 IP 與靜音說明"
    assert "x!=='ip_differs'" in page.replace(" ", ""), "名單的標記與『有標記』篩選要排除 ip_differs"
    home = h.T.get("/t").text
    assert "名單標「IP 與教室不同」" not in home and "只存不顯示" in home


# 舊場次入口

def test_report_links_every_session_and_home_links_to_report(h):
    sids = []
    for _ in range(8):
        sid = h.open("CD")
        _wording_close(h, sid)
        sids.append(sid)
    cid = h.cid["CD"]
    rep = h.T.get(f"/t/course/{cid}/report")
    assert rep.status_code == 200
    linked = [int(x) for x in re.findall(r'href="/t/session/(\d+)"', rep.text)]
    assert sorted(set(linked)) == sorted(sids), (linked, sids)
    for sid in sids:
        assert h.T.get(f"/t/session/{sid}").status_code == 200
    home = h.T.get("/t").text
    assert f'href="/t/course/{cid}/report">全部場次（8）' in home.replace("\n", ""), "主頁每門課場次列表下方要有「全部場次（N）」連到報表"
    # R4：N 與報表頁實際列出的場次欄數同一個母體（綁定場次、已作廢的不算）
    sid_b = h.open("CD", kind="bind")
    _wording_close(h, sid_b)
    sid_v = h.open("CD")
    _wording_close(h, sid_v)
    h.T.post(f"/api/t/session/{sid_v}/void", data={"void": "1"})
    n_cols = len(re.findall(r'<th class="d mono"><a href="/t/session/\d+">', h.T.get(f"/t/course/{cid}/report").text))
    home = h.T.get("/t").text.replace("\n", "")
    assert n_cols == 8 and f'href="/t/course/{cid}/report">全部場次（{n_cols}）' in home, (n_cols, "綁定與作廢場次不算進 N")


# 限流的口子

def _req(ip):
    from starlette.requests import Request
    return Request({"type": "http", "headers": [(b"cf-connecting-ip", ip.encode())] if ip else [], "client": None,
                    "method": "GET", "path": "/", "query_string": b""})


def test_1_rate_key_uses_ipv6_slash_64_only_for_limits(h):
    A = h.A
    assert A.rate_ip(_req("2001:db8:1:2::5")) == A.rate_ip(_req("2001:db8:1:2:ffff:1:2:3"))
    assert A.rate_ip(_req("2001:db8:1:2::5")) != A.rate_ip(_req("2001:db8:1:3::5"))
    assert A.rate_ip(_req("140.114.1.1")) == "140.114.1.1" and A.rate_ip(_req("")) == "noip"
    assert A.client_ip(_req("2001:db8:1:2::5")) == "2001:db8:1:2::5", "client_ip 與寫進資料庫的 IP 不變"


def test_1_code_page_cap_applies_across_a_rotating_ipv6_slash_64(h):
    h.open("CD")
    cl = h.student()
    n429 = 0
    for i in range(400):
        cl.cookies.clear()
        r = cl.post("/c", data={"code": f"{(i * 7919) % 1000000:06d}"},
                    headers={"cf-connecting-ip": f"2001:db8:1:2::{i // 50 + 1:x}", "user-agent": "x"})
        n429 += r.status_code == 429
    assert n429 > 0, "同一個 /64 換位址亂猜，總量要被每出口桶擋下"


def test_2_code_page_exit_bucket_spares_correct_code_and_bound_devices(h):
    sid = h.open("CD")
    assert h.checkin(sid, "dev26-bound", "9994A001", name="學生1", pin="1234")["ok"]
    atk = h.student()
    for i in range(300):
        atk.cookies.clear()
        atk.post("/c", data={"code": f"{i:06d}"}, headers={"cf-connecting-ip": "140.114.1.1", "user-agent": "curl/8"})
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    nat = {"cf-connecting-ip": "140.114.1.1", "user-agent": "iPhone"}
    honest = h.student(); honest.get("/c")
    assert honest.post("/c", data={"code": code}, headers=nat).status_code == 200, "正確的代碼不被每出口桶擋"
    stranger = h.student(); stranger.get("/c")
    assert stranger.post("/c", data={"code": "000001"}, headers=nat).status_code == 429, "錯碼仍受每出口桶限制"
    bound = h.dev("dev26-bound")
    r = bound.post("/c", data={"code": "000001"}, headers=nat)
    assert r.status_code == 400, "已綁裝置不受每出口桶限制（只受自己那桶）"


def test_3_code_bad_attached_to_the_only_live_session_and_shown_in_funnel(h):
    sid = h.open("CD")
    cl = h.student()
    for i in range(3):
        assert cl.post("/c", data={"code": f"00000{i}"}, headers={"cf-connecting-ip": "140.114.2.2"}).status_code == 400
    with h.A.db() as con:
        sids = [r[0] for r in con.execute("SELECT session_id FROM events WHERE kind='code_bad'")]
    assert sids == [sid] * 3, sids
    st = h.state(sid)
    assert st["funnel"]["total"]["code_bad"] == 3 and st["funnel"]["recent60"]["code_bad"] == 3, st["funnel"]


def test_3_code_bad_not_attached_when_two_sessions_live(h):
    h.open("CD"); h.open("DT")
    h.student().post("/c", data={"code": "000000"})
    with h.A.db() as con:
        sids = [r[0] for r in con.execute("SELECT session_id FROM events WHERE kind='code_bad'")]
    assert sids == [None], sids


def test_4_landing_without_cookie_is_rate_limited_per_ip_and_not_counted_as_filling(h):
    sid = h.open("CD")
    url = _student_entry_url(h, sid)
    cl = h.student()
    codes = []
    for _ in range(130):
        cl.cookies.clear()
        codes.append(cl.get(url, headers={"cf-connecting-ip": "2001:db8:9:9::1", "user-agent": "curl/8"}).status_code)
    assert codes.count(200) == 120 and codes.count(429) == 10, (codes.count(200), codes.count(429))
    r = cl.get(url, headers={"cf-connecting-ip": "2001:db8:9:9::ffff", "user-agent": "curl/8"})  # 同一個 /64
    assert r.status_code == 429 and "太頻繁，等一下再掃" in r.text, (r.status_code, r.text[:200])
    with h.A.db() as con:
        n_land = con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND kind='land_ok'", (sid,)).fetchone()[0]
    assert n_land == 120, n_land
    st = h.state(sid)
    assert st["filling"] == 0, ("沒有帶回 cookie 的落地不算在填表", st["filling"])
    # 之後帶回 cookie 的裝置才算：這支手機落地後 whoami 帶著 cookie 回來
    phone = h.student()
    assert phone.get(url, headers={"cf-connecting-ip": "203.0.113.7", "user-agent": "iPhone"}).status_code == 200
    assert phone.post("/api/whoami", data={"course_id": h.cid["CD"]}).status_code == 200
    assert h.state(sid)["filling"] == 1


def test_4_landing_with_cookie_is_not_limited(h):
    sid = h.open("CD")
    url = _student_entry_url(h, sid)
    cl = h.dev("dev26-cookie")
    codes = {cl.get(url, headers={"cf-connecting-ip": "2001:db8:8::1", "user-agent": "iPhone"}).status_code for _ in range(130)}
    assert codes == {200}, codes


# 主頁場次列「n_ok／人數」對得上投影幕與 Discord

def test_home_session_row_matches_projector_and_discord(h):
    sid = h.open("CD")
    assert h.checkin(sid, "dev29-a", "9994A001", name="學生1", ua="UA-1")["ok"]
    with h.A.db() as con:
        opened = h.A.get_session(con, sid)["opened_at"]
        pk2 = con.execute("SELECT id FROM students WHERE student_id='9994A002' AND course_id=?", (h.cid["CD"],)).fetchone()["id"]
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,'qr')", (sid, pk2, opened + 1, "pending", "[]"))
        con.execute("INSERT INTO students(course_id,student_id,name,cls,created_at) VALUES(?,?,?,?,?)", (h.cid["CD"], "9994A099", "加選生", "", opened + 5))
    qr = h.T.get(f"/api/t/session/{sid}/qr").json()
    assert (qr["n_ok"], qr["total"]) == (1, 4), qr  # 加選生不在母體
    _chan, text = _summary(h, sid)
    assert "出席 1" in text and "待確認 1" in text and "全班 4 人" in text, text
    home = h.T.get("/t").text
    assert re.search(r'<td class="mono">1 / 4（待確認 1）</td>', home), re.findall(r'<td class="mono">[^<]*/[^<]*</td>', home)


def _summary(h, sid):
    with h.A.db() as con:
        return h.A.session_summary(con, sid)


# 送出時網路失敗的學生老師頁看得到

def test_neterr_device_counts_as_net_fail_not_silent_until_it_succeeds(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        url = _student_entry_url(h, sid)
        for dev in ("dev30-net", "dev30-quiet"):
            assert h.dev(dev).get(url, headers={"user-agent": "iPhone"}).status_code == 200
        assert h.dev("dev30-net").post("/api/beacon", data={"kind": "neterr", "msg": "http 502", "sid": str(sid)}).status_code == 204
        tr.shift(100)
        f = h.state(sid)["funnel"]["total"]
        assert f["net_fail"] == 1 and f["silent"] == 1, f  # 沒回報的那支照舊算沉默，回報 neterr 的那支改算送出失敗
        assert h.checkin(sid, "dev30-net", "9994A001", name="學生1")["ok"]
        f = h.state(sid)["funnel"]["total"]
        assert f["net_fail"] == 0 and f["silent"] == 1, f


# Discord 失敗訊息

_TIMEOUT = "Discord 沒有回應，名單可能已經發出；先到頻道看一下再決定要不要重發"


def _raise(monkeypatch, exc):
    import urllib.request

    def fake(req, timeout=0):
        raise exc
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_discord_post_failure_messages(h, monkeypatch):
    import io, socket, urllib.error

    def http(code):
        return urllib.error.HTTPError("https://discord.com/x", code, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type]
    h.A.DISCORD_TOKEN = "tok"
    cases = [
        (socket.timeout("timed out"), _TIMEOUT),
        (TimeoutError("x"), _TIMEOUT),
        (urllib.error.URLError(socket.timeout("timed out")), _TIMEOUT),
        (http(401), "Discord 機器人權限不足（HTTP 401）"),
        (http(403), "Discord 機器人權限不足（HTTP 403）"),
        (http(429), "Discord 限流中，稍後再發"),
        (http(500), "Discord 發送失敗（HTTP 500）"),
        (OSError("boom"), "Discord 發送失敗（OSError）"),
        (urllib.error.URLError(ConnectionRefusedError()), "Discord 發送失敗（URLError）"),
    ]
    for exc, want in cases:
        _raise(monkeypatch, exc)
        ok, msg = h.A.discord_post("42", "x")
        assert (ok, msg) == (False, want), (exc, msg)
        assert "urlopen error" not in msg and "boom" not in msg


def test_notify_returns_the_message_once_even_for_several_chunks(h, monkeypatch):
    h.A.DISCORD_TOKEN = "tok"
    _course_settings(h, "CD", discord_channel="123456789012345")
    with h.A.db() as con:
        for i in range(400):  # 缺席名單長到要切成多則
            con.execute("INSERT INTO students(course_id,student_id,name,cls,created_at) VALUES(?,?,?,?,0)", (h.cid["CD"], f"X{i:04d}", f"長名單同學{i}", ""))
    sid = h.open("CD")
    _wording_close(h, sid)
    import socket
    _raise(monkeypatch, socket.timeout("timed out"))
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 424 and r.json()["error"] == _TIMEOUT, r.text


# 按「先不要自動發」後自動結案，主頁要看得到

def test_home_shows_held_tag_after_hold_then_auto_close(h, monkeypatch):
    _fake_discord(h, monkeypatch)
    _course_settings(h, "CD", discord_channel="123456789012345")
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        assert "Discord 未發（暫緩中）" not in h.T.get("/t").text
        assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}).json()["held"] is True
        tr.shift(16 * 60)  # 遲到窗過了，下次讀取時自動結案
        home = h.T.get("/t").text
        assert _sess(h, sid)["closed_at"] is not None and _sess(h, sid)["note"] == "discord_held_manual"
        assert "Discord 未發（暫緩中）" in home
        _course_settings(h, "CD", discord_channel="")  # 沒頻道：不顯示
        assert "Discord 未發（暫緩中）" not in h.T.get("/t").text
        _course_settings(h, "CD", discord_channel="123456789012345")
        h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})  # 已作廢：不顯示
        assert "Discord 未發（暫緩中）" not in h.T.get("/t").text


# 學生端非預期例外是中文頁

def test_unhandled_error_on_student_route_is_chinese_page_with_event(h, monkeypatch):
    from fastapi.testclient import TestClient
    sid = h.open("CD")
    url = _student_entry_url(h, sid)
    cl = TestClient(h.A.app, base_url="http://test", raise_server_exceptions=False)
    cl.get("/c")
    monkeypatch.setattr(h.A, "session_phase", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    r = cl.get(url)
    assert r.status_code == 500 and "系統出了點問題，請重新掃 QR；還是不行請找老師" in r.text, r.text[:300]
    assert 'href="/help">主頁</a>' in r.text and "Internal Server Error" not in r.text
    with h.A.db() as con:
        evs = [dict(x) for x in con.execute("SELECT session_id, reason FROM events WHERE kind='http_err'")]
    assert evs == [{"session_id": sid, "reason": "500 /s/{sid}/{slot}/{sig}"}], evs  # 只記路由樣式，不含簽章與個資


def test_unhandled_error_on_api_route_stays_json_and_teacher_page_links_home(h, monkeypatch):
    from fastapi.testclient import TestClient
    cl = TestClient(h.A.app, base_url="http://test", raise_server_exceptions=False)
    cl.get("/c")
    monkeypatch.setattr(h.A, "_throttle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = cl.post("/api/me")
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json") and r.json()["ok"] is False, r.text
    monkeypatch.undo()
    with h.A.db() as con:
        before = con.execute("SELECT COUNT(*) FROM events WHERE kind='http_err'").fetchone()[0]
    assert before == 1, "/api/me 是學生端路由：記一筆"
    t = TestClient(h.A.app, base_url="http://test", raise_server_exceptions=False)
    t.cookies.set("ta", h.T.cookies.get("ta"))
    monkeypatch.setattr(h.A, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = t.get(f"/t/course/{h.cid['CD']}/report")
    assert r.status_code == 500 and "系統出了點問題" in r.text and 'href="/t">主頁</a>' in r.text, r.text[:300]
    with h.A.db() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE kind='http_err'").fetchone()[0] == before, "老師端不記學生端錯誤事件"


# CSV 公式注入也擋 Tab／CR

def test_csv_safe_prefixes_tab_and_cr(h):
    for lead in ("=", "+", "-", "@", "\t", "\r"):
        assert h.A.csv_safe(lead + "1+1") == "'" + lead + "1+1", repr(lead)
    assert h.A.csv_safe("王小明") == "王小明" and h.A.csv_safe("") == ""


# 背景發送失敗時老師頁的紅字要照原因說（逾時＝名單可能已經發出）

def _bg_fail(h, monkeypatch, result=None, exc=None):
    sid = h.open("CD")
    def fake(ch, text):
        if exc is not None:
            raise exc
        return result
    monkeypatch.setattr(h.A, "discord_post_long", fake)
    h.A._bg_discord_post(sid, "1", "名單")
    return sid


def test_bg_timeout_banner_says_list_may_have_been_sent(h, monkeypatch):
    sid = _bg_fail(h, monkeypatch, result=(False, h.A._discord_fail_msg(exc=TimeoutError())))
    st = h.state(sid)
    assert st["discord_failed"] is True
    assert "可能已經發出" in st["discord_fail_text"] and "先到頻道" in st["discord_fail_text"], st["discord_fail_text"]
    assert "沒有成功" not in st["discord_fail_text"] and "權限" not in st["discord_fail_text"], st["discord_fail_text"]


def test_bg_http_error_banner_names_the_reason(h, monkeypatch):
    sid = _bg_fail(h, monkeypatch, result=(False, h.A._discord_fail_msg(status=403)))
    t = h.state(sid)["discord_fail_text"]
    assert "沒有成功" in t and "權限不足（HTTP 403）" in t and "發到 Discord" in t, t


def test_bg_exception_logged_without_raw_text(h, monkeypatch):
    sid = _bg_fail(h, monkeypatch, exc=OSError("boom-raw-english"))
    with h.A.db() as con:
        r = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='discord_fail'", (sid,)).fetchone()["reason"]
    assert r == "Discord 發送失敗（OSError）" and "boom" not in r, r
    assert "boom" not in h.state(sid)["discord_fail_text"]


def test_no_banner_text_when_not_failed(h):
    sid = h.open("CD")
    assert h.state(sid)["discord_fail_text"] == ""


# 突變判讀補的斷言

def _fail_text(h, sid):
    with h.A.db() as con:
        return h.A.discord_fail_text(con, h.A.get_session(con, sid))


def test_fail_text_without_event_falls_back_instead_of_crashing(h):
    """note＝discord_failed 但找不到 discord_fail 事件（例如舊資料）：給通用說法，不能因為查無事件而丟例外讓老師頁 500。"""
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET note='discord_failed' WHERE id=?", (sid,))
    t = _fail_text(h, sid)
    assert "沒有成功" in t and "可能是機器人權限或頻道設定有問題" in t, t


def test_fail_text_hides_old_english_raw_reason(h):
    """事件原因是英文例外原文時（較早的資料），老師頁不顯示原文，改給通用說法（discord_fail_text 的說明）。"""
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET note='discord_failed' WHERE id=?", (sid,))
    h.A.log_event(sid, "discord_fail", reason="URLError: <urlopen error timed out>")
    t = _fail_text(h, sid)
    assert "URLError" not in t and "urlopen" not in t and "可能是機器人權限或頻道設定有問題" in t, t


def test_bg_failure_without_exception_logs_one_clean_stderr_line(h, monkeypatch, capsys):
    msg = h.A._discord_fail_msg(status=403)
    sid = _bg_fail(h, monkeypatch, result=(False, msg))
    assert capsys.readouterr().err == f"[discord] 場次 {sid} 背景發送失敗：{msg}\n"


def test_bg_exception_stderr_keeps_exception_type_and_text(h, monkeypatch, capsys):
    """例外原文只留在伺服器日誌：類別名與訊息都要在，查問題時才分得出是逾時、連線被拒還是別的。"""
    sid = _bg_fail(h, monkeypatch, exc=OSError("boom-raw-english"))
    err = capsys.readouterr().err
    assert err.startswith(f"[discord] 場次 {sid} 背景發送失敗：Discord 發送失敗（OSError）") and "（OSError: boom-raw-english）" in err, err


def test_post_long_joins_distinct_reasons_once_each(h, monkeypatch):
    reasons = iter(["Discord 機器人權限不足（HTTP 403）", "Discord 限流中，稍後再發"])
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (False, next(reasons)))
    ok, msg = h.A.discord_post_long("1", "a" * 1000 + "\n" + "b" * 1000)  # 兩則
    assert (ok, msg) == (False, "Discord 機器人權限不足（HTTP 403）; Discord 限流中，稍後再發"), msg


def test_same_fp_note_says_same_network_and_matches_receipt(h):
    """同指紋轉待確認的說明不能退回「這支手機簽過別的學號」；回訪頁（伺服器）與收據（前端）同一句話。"""
    note = h.A.pending_reason_note("same_fp")
    assert "同一個網路" in note and note != h.A.pending_reason_note(None), note
    import os
    page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "student.html"), encoding="utf-8").read()
    assert note in page, "收據（student.html）與回訪頁的 same_fp 說明要一致"


def test_csv_safe_leaves_ordinary_values_alone(h):
    for plain in ("Xavier", "abc", "9994A001", "王小明", "'already"):
        assert h.A.csv_safe(plain) == plain, plain


def test_error_page_on_exact_teacher_root_links_teacher_home_and_shows_status(h):
    """路徑剛好是 /t（不是 /t/…）出錯時，錯誤頁的「主頁」也連老師首頁；頁面要寫出狀態碼。"""
    r = h.T.request("PUT", "/t")
    assert r.status_code == 405 and r.headers["content-type"].startswith("text/html"), r.text[:200]
    assert '<a href="/t">主頁</a>' in r.text and "<h1>405</h1>" in r.text, r.text[:400]
    s = h.student().request("PUT", "/c")  # 對照：學生端路徑連 /help
    assert s.status_code == 405 and '<a href="/help">主頁</a>' in s.text and "<h1>405</h1>" in s.text, s.text[:400]
