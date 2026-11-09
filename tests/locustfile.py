"""壓測：N 個學生在同一場點名裡同時掃碼簽到（跑法見 tests/README.md）。
需要先有一場進行中的點名；老師 cookie 由環境變數 ATTEND_TA 提供（用來拿當下六位數）。"""
import os, re, random, itertools
from locust import HttpUser, task, between
from locust.exception import StopUser

SID = int(os.environ.get("ATTEND_SID", "1"))
TA = os.environ["ATTEND_TA"]
IDS = itertools.cycle([f"9994A{n:03d}" for n in range(1, 49) if n != 18])
NAMES = {}


class Student(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self):
        self.sid = next(IDS)
        self.dev = f"locust-{self.sid}-{random.random()}-xxxxxxxx"
        if not NAMES:
            import csv
            for r in csv.DictReader(open(os.environ["ATTEND_ROSTER"], encoding="utf-8")):
                NAMES[r["student_id"]] = r["name"]

    @task
    def checkin(self):
        code = self.client.get(f"/api/t/session/{SID}/qr", cookies={"ta": TA}, name="/api/t/qr").json()["code"]
        html = self.client.post("/c", data={"code": code}, name="/c").text
        m = re.search(r'GRANT="([^"]+)"', html)
        if not m:
            return
        r = self.client.post("/api/checkin", data={"grant": m.group(1), "device_key": self.dev, "student_id": self.sid,
                                                   "name": NAMES.get(self.sid, ""), "pin": "1234", "pin2": "1234", "hints": "h"}, name="/api/checkin")
        if r.ok:
            raise StopUser()  # 真實情境：簽到成功就收手機；每支手機一分鐘最多 12 次（限流）


from locust import events

@events.quitting.add_listener
def _gate(environment, **kw):
    s = environment.stats.get("/api/checkin", "POST")
    fail = environment.stats.total.num_failures
    # 首簽成功率門檻：5xx 或連線失敗超過 1% 就讓程序以非零離開（4xx 在 locust 預設也算 failure，所以名冊要 ≥ 使用者數）
    if s.num_requests and fail / max(1, environment.stats.total.num_requests) > 0.01:
        environment.process_exit_code = 1
