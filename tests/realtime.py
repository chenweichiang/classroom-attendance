"""真實時間流逝測試（約 3 分半）：出席窗 1 分鐘、遲到窗 2 分鐘、QR 10 秒輪換＋5 秒容許，不改資料庫時間戳。
跑法：乾淨庫啟本機 uvicorn（ATTEND_TEST_BASE，預設 8765）＋匯入 NTUB-CD 後 .venv/bin/python tests/realtime.py"""
import os, re, sys, time, requests

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765"); FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + f"[{time.strftime('%H:%M:%S')}] " + m)
    if not c: FAIL.append(m)
T = requests.Session(); T.post(f"{B}/t/login", data={"password": "pw", "next": "/t"})
T.post(f"{B}/t/course/1/settings", data={"open_minutes": 1, "late_minutes": 2, "qr_interval": 10, "qr_grace": 5, "spot_n": 3, "ip_check": "1", "discord_channel": ""})
sid = int(T.post(f"{B}/t/course/1/open", allow_redirects=False).headers["location"].rsplit("/", 1)[1])
t0 = time.time()
def code(): return T.get(f"{B}/api/t/session/{sid}/qr").json()
_devs = {}
def dev(name):  # 一支手機＝一個保留 cookie 的 session（識別由伺服器下發）
    if name not in _devs:
        _devs[name] = requests.Session(); _devs[name].get(f"{B}/c")
    return _devs[name]
def grant(c, d="x"): return re.search(r'GRANT="([^"]+)"', dev(d).post(f"{B}/c", data={"code": c}).text).group(1)
def sign(g, d, stu, name): return dev(d).post(f"{B}/api/checkin", data={"grant": g, "student_id": stu, "name": name, "pin": "1234", "pin2": "1234"}).json()
# QR 輪換：等到一片剛開始，拿碼，等 14 秒仍可用（10 秒＋5 秒容許），等到 16 秒不可用
while code()["remain"] < 9: time.sleep(0.2)
c = code()["code"]; t = time.time()
time.sleep(13.5); r = requests.post(f"{B}/c", data={"code": c}); check(r.status_code == 200, f"碼在第 {time.time()-t:.1f} 秒仍可用（容許期內）")
while code()["remain"] < 9: time.sleep(0.2)
c = code()["code"]; t = time.time(); time.sleep(16); r = requests.post(f"{B}/c", data={"code": c}); check(r.status_code == 400, f"碼在第 {time.time()-t:.1f} 秒失效")
# 開場簽到（grant 過期在 test_api 用假時間測）
j = sign(grant(code()["code"], "a"), "a", "9994A001", "陳大文"); check(j["status"] == "出席", f"開場 {time.time()-t0:.0f} 秒簽到＝出席")
# 等出席窗過（1 分鐘）
time.sleep(max(0, 62 - (time.time() - t0)))
st = T.get(f"{B}/api/t/session/{sid}/state").json(); check(st["phase"] == "late", f"{time.time()-t0:.0f} 秒 phase=late")
j = sign(grant(code()["code"], "b"), "b", "9994A002", "吳家豪"); check(j["status"] == "遲到", f"{time.time()-t0:.0f} 秒簽到＝遲到")
# 遲到區按延長：只延遲到窗，不重開出席窗（晚到的人不會反而拿到出席）
r = T.post(f"{B}/api/t/session/{sid}/extend", data={"minutes": 1}).json(); st = T.get(f"{B}/api/t/session/{sid}/state").json(); check(r["phase"] == "late" and st["phase"] == "late", "遲到區延長後仍在遲到區")
j = sign(grant(code()["code"], "c"), "c", "9994A003", "林小華"); check(j["status"] == "遲到", "延長期間簽到＝遲到")
# 等到遲到窗也過（原 2 分鐘＋延長 1 分鐘）
while T.get(f"{B}/api/t/session/{sid}/state").json()["phase"] != "closed":
    time.sleep(2)
    if time.time() - t0 > 400: break
check(T.get(f"{B}/api/t/session/{sid}/state").json()["phase"] == "closed", f"{time.time()-t0:.0f} 秒自動進入已結束")
r = requests.post(f"{B}/c", data={"code": code()["code"]}); check(r.status_code == 400, "結束後輸碼被拒")
rows = T.get(f"{B}/api/t/session/{sid}/state").json()["rows"]; st = {r["student_id"]: r["status"] for r in rows}
check(st["9994A001"] == "present" and st["9994A002"] == "late" and st["9994A003"] == "late" and st["9994A004"] == "absent", f"自動結案後狀態 {st['9994A001']},{st['9994A002']},{st['9994A003']},{st['9994A004']}")
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
