"""這是探針，不是一般測試（檔名不以 test_ 開頭，預設不被收集），只由 test_harness.py 的子行程 pytest 依固定順序指名執行。
a 結束點名時起 Discord 背景執行緒（discord_post 換成睡 0.3 秒的假函式，模擬機器忙時它晚一點才跑完）、測試立刻結束；
b 是下一個測試，開始時 a 的執行緒不准還活著，它的庫裡也不准有 a 的 discord 事件。
c 的執行緒卡住不結束：conftest 要在逾時時讓這個測試失敗並印出執行緒名稱。"""
import json
import threading
import time

_RELEASE = threading.Event()


def _channel(h):
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))


def _close_with_background_post(h, monkeypatch, fake):
    monkeypatch.setattr(h.A, "discord_post", fake)
    _channel(h)
    sid = h.open("CD")
    for i in (1, 2, 3):  # 出席不到一半會暫緩發送，要三個人
        h.checkin(sid, f"probe-{i}", f"9994A00{i}", name=f"學生{i}", pin="1234")
    assert h.T.post(f"/api/t/session/{sid}/close").json()["discord"] is True  # 背景執行緒已起


def test_a_spawns_slow_background_post(h, monkeypatch):
    _close_with_background_post(h, monkeypatch, lambda ch, c: (time.sleep(0.3), (True, "HTTP 200"))[1])


def _alive_discord_threads():  # 自己認，不借 conftest 的函式（要能拿去對沒有修正的舊 conftest 跑出紅燈）
    return [t.name for t in threading.enumerate()
            if t.is_alive() and "_bg_discord_post" in getattr(getattr(getattr(t, "_target", None), "__code__", None), "co_names", ())]


def test_b_starts_clean(h):
    alive = _alive_discord_threads()
    assert alive == [], f"上一個測試的 Discord 背景執行緒還活著：{alive}"
    with h.A.db() as con:
        ev = [dict(r) for r in con.execute("SELECT session_id, kind FROM events WHERE kind LIKE 'discord%'")]
    assert ev == [], f"上一個測試的背景執行緒寫進了這個測試的庫：{ev}"


def test_c_stuck_background_post(h, monkeypatch):
    _close_with_background_post(h, monkeypatch, lambda ch, c: (_RELEASE.wait(30), (True, "HTTP 200"))[1])


def test_zz_release():
    _RELEASE.set()
