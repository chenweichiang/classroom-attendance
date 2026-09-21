"""API 與頁面全覆蓋：每個端點在每個 phase 的行為、錯誤路徑、對帳。"""
import time, csv, io, threading


def test_login_flow(client):
    assert client.get("/t", follow_redirects=False).status_code == 303
    assert client.get("/api/t/session/1/state").status_code == 401
    r = client.post("/t/login", data={"password": "中文亂打", "next": "/t"}); assert r.status_code == 401 and "密碼錯誤" in r.text
    r = client.post("/t/login", data={"password": "pw", "next": "//evil.com"}, follow_redirects=False); assert r.headers["location"] == "/t"
    r = client.post("/t/login", data={"password": "pw", "next": "/t/course/1/students"}, follow_redirects=False); assert r.headers["location"] == "/t/course/1/students"
    assert client.get("/t", follow_redirects=False).status_code == 200
    assert client.get("/t/logout", follow_redirects=False).status_code == 303
    assert client.get("/t", follow_redirects=False).status_code == 303


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
    a.get("/t/logout", follow_redirects=False)
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
    fr = client.get("/favicon.ico"); assert fr.status_code == 204 and "max-age" in fr.headers["cache-control"]  # B012
    # B062：字型網址要帶內容雜湊，重做子集後 Cloudflare 邊緣與瀏覽器的七天快取才會自動失效
    import hashlib, pathlib
    font = pathlib.Path(__file__).resolve().parent.parent / "static" / "fonts" / "ZhuqueFangsong-subset.woff2"
    v = hashlib.sha256(font.read_bytes()).hexdigest()[:10]
    assert f"ZhuqueFangsong-subset.woff2?v={v})" in client.get("/help").text
    assert client.get(f"/static/fonts/ZhuqueFangsong-subset.woff2?v={v}").status_code == 200


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
    # 同一個校園出口 IP 上，有裝置 cookie 的甲瀏覽器輸錯 10 次被擋（B021：per-device 限流不變）
    dev = h.student(); dev.get("/c")  # 先拿伺服器下發的裝置 cookie
    for _ in range(10):
        dev.post("/c", data={"code": "000000"}, headers={"cf-connecting-ip": "198.51.100.77", "user-agent": "A"})
    assert dev.post("/c", data={"code": "000000"}, headers={"cf-connecting-ip": "198.51.100.77", "user-agent": "A"}).status_code == 429
    # B021：沒有 cookie（退回指紋）的一批人共用同一個放寬到 IP 同級的額度，10 次內不會被擋
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
    # C-S5：五段憑證簽章正確，但第四段（issued）不是數字——防禦性 ValueError 分支，一樣視為過期
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
    # B016：只要請求帶了 pin2，一律回「已經設過 PIN」，不看剩幾次；失敗計次照舊要計，
    # 故第 4 次不管 PIN 對不對都已經鎖了——鎖定本身在下一行用正確 PIN 也拿 423 獨立驗證）
    g2 = h.grant(sid, h.dev("Z"))
    for i in range(3):
        r = post(dev="Z", grant=g2, pin="9999", pin2="9999")
        assert r.status_code == 401 and "已經設過 PIN" in r.json()["error"]
    assert post(dev="Z", grant=g2).status_code == 423  # 鎖定中（這次帶的是正確 PIN，仍被早鎖擋下）
    # grant 過期／未過期
    assert post(grant=h.A.make_token("g", str(sid), "n", exp=h.A.now() - 1)).status_code == 410
    assert "填表時間" in post(grant=h.A.make_token("g", str(sid), "n", exp=h.A.now() - 1)).json()["error"]
    # 已結束：未鎖定的學生恆 410
    h.T.post(f"/api/t/session/{sid}/close")
    # A3/B029：grant 多帶一段發證時間（g, sid, nonce, issued）；C-S5：再多帶一段送出裝置的雜湊前 16 碼，
    # 四段式（無裝置雜湊）與三段式舊格式一律視為無效不相容
    d_dev = h.dev("D")
    dh16 = h.A.hash_device(d_dev.cookies.get("attend_dk"))[:16]
    r = post(dev="D", grant=h.A.make_token("g", str(sid), "z", str(h.A.now()), dh16, exp=h.A.now() + 60),
             student_id="9994A002", name="學生2")
    assert r.status_code == 410 and "點名已結束" in r.json()["error"]


def test_already_manual_row_gets_conflict_flags_but_keeps_status(h):
    """B022/B037：老師手動設過狀態後，借用他人已綁定裝置以該學號重新「首次綁定」——已經有紀錄那一列
    要補上 device_bound_other／same_device 讓老師看到，但狀態不能被改掉（app.py 1170-1183）。"""
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
    PIN 錯誤照舊走「剩 N 次」倒數、最後鎖定，不是 B016 那句解釋訊息（app.py 1157-1158）。
    A2：已綁裝置回簽免 PIN，這裡改用另一支從未綁定過的裝置「E2」才吃得到 PIN 驗證。"""
    sid = h.open("CD")
    g = h.grant(sid, h.dev("E"))
    r = h.dev("E").post("/api/checkin", data={"grant": g, "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234"})
    assert r.json()["ok"]
    for left in (2, 1):
        r = h.dev("E2").post("/api/checkin", data={"grant": h.grant(sid, h.dev("E2")), "student_id": "9994A001", "pin": "0000"})
        assert r.status_code == 401 and f"剩 {left} 次" in r.json()["error"]
    r = h.dev("E2").post("/api/checkin", data={"grant": h.grant(sid, h.dev("E2")), "student_id": "9994A001", "pin": "0000"})
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
    assert h.T.post(f"/api/t/session/{sid}/notify").status_code == 502  # 沒 token
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
    assert st == {**st, "open_minutes": 1, "late_minutes": 1, "qr_interval": 5, "qr_grace": 99, "spot_n": 1, "ip_check": False, "discord_channel": "12345678901234567", "allow_bind": True}  # B015：qr_grace 上限 120，不再夾在 qr_interval 以內
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
    h.checkin(sid, "d1-1234567890123456", "9994A001", "學生1")
    assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True
    for _ in range(50):
        if sent: break
        time.sleep(0.05)
    assert sent and sent[0][0] == "42" and "缺席 3" in sent[0][1] and "9994A002 學生2" in sent[0][1]
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


# ───────────────────────── A1（SPEC 2026-09-21）：manage.py set-setting ─────────────────────────
# 既有的 `manage.py set --code <代碼> --key <鍵> --value <值>`（見上方 test_manage_cli）就是規格說的
# 「set-setting」子指令（沿用既有 --code/--key/--value 介面，未改名，見回報說明）；這裡補規格點名的
# 三種情境：合法值寫入、非法值被 course_settings 的驗證拉回、不存在的課程代碼報錯。

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


def test_manage_set_out_of_range_gets_clamped_back_by_course_settings(A):
    """manage.py 對 spot_n 沒有自己的下限檢查，值原樣寫進 DB；讀出來時 course_settings() 的驗證會拉回下限。"""
    import json, os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = _manage_run(base, {**os.environ, "ATTEND_DB": os.environ["ATTEND_DB"]})
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('SET2','A1 拉回')")
    r = run("set", "--code", "SET2", "--key", "spot_n", "--value", "0")
    assert r.returncode == 0, r.stderr
    with A.db() as con:
        cid = con.execute("SELECT id FROM courses WHERE code='SET2'").fetchone()[0]
        raw = json.loads(con.execute("SELECT settings FROM courses WHERE id=?", (cid,)).fetchone()[0])
        st = A.course_settings(A.get_course(con, cid))
    assert raw["spot_n"] == 0, "manage.py 寫入時沒有主動拉回，DB 裡是原始值"
    assert st["spot_n"] == 1, "course_settings() 讀出來時驗證把下限拉回 1"


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
    d = h.db_rows(sid); assert "same_fp" in d["9994A001"]["flags"] and "same_fp" in d["9994A002"]["flags"]
    assert d["9994A002"]["status"] == "present"  # same_fp 只標記
    j = h.checkin(sid, "dC-1234567890123456", "9994A003", "學生3", ua="iPhone/1")  # 同來源兩分鐘內第三筆首綁 → 只標記
    assert j["status"] == "出席" and "bind_burst" in h.db_rows(sid)["9994A003"]["flags"]
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
    assert s["late_minutes"] >= s["open_minutes"] >= 1 and s["qr_interval"] >= 5 and 0 <= s["qr_grace"] <= 120  # B015：新上限語意


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
    for _ in range(3):
        h.checkin(sid, "evil-1234567890123456", "9994A001", pin="0000")
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
        res.append(devs[i].post("/api/checkin", data={"grant": grants[i], "student_id": ids[i], "name": "學生" + ids[i][-1], "pin": "1234", "pin2": "1234"}).json())
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
    """一個不留 cookie 的攻擊者替全班簽：每次拿到新 cookie＝新裝置，但同來源多筆首綁會被停在待確認。"""
    sid = h.open("CD"); results = []
    for stu, name in (("9994A001", "學生1"), ("9994A002", "學生2"), ("9994A003", "學生3"), ("9994A004", "學生4")):
        S = h.student()  # 每次全新 client（等同不回送 cookie）
        g = h.grant(sid, S)
        results.append(S.post("/api/checkin", data={"grant": g, "student_id": stu, "name": name, "pin": "1234", "pin2": "1234"}, headers={"user-agent": "bot", "cf-connecting-ip": "203.0.113.9"}).json())
    st = [r["status"] for r in results]
    assert st == ["出席"] * 4, st  # 不改狀態（第一週集體綁定會誤傷），但第三筆起標記
    with h.A.db() as con:
        flagged = [r["student_id"] for r in con.execute("SELECT st.student_id FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=? AND k.flags LIKE '%bind_burst%' ORDER BY st.student_id", (sid,))]
    assert flagged == ["9994A003", "9994A004"]


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
