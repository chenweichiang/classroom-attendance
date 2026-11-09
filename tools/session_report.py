"""唯讀場次診斷報告：讀事件表（events）與簽到表（checkins），把一場點名裡「誰在什麼時候卡在哪一步」排成時間線。

輸出匿名化：學生一律用 S01、S02…（同一次執行內一致，跨次執行不保證相同），裝置用雜湊前 4 碼，
不印學號、姓名、IP、PIN。只開唯讀連線（SQLite URI `mode=ro`），不寫入任何東西。

正式站（容器根目錄唯讀，從標準輸入餵進去；參數接在 `python -` 後面）：
  docker compose exec -T attend python - 9 < tools/session_report.py
  docker compose exec -T attend python - --last 3 < tools/session_report.py
本機：
  ATTEND_DB=/路徑/attend.db python tools/session_report.py 9

不給場次號＝最近 3 場。資料庫路徑照 app.py 的規則：ATTEND_DB → /data/attend.db。
"""
import collections
import datetime
import json
import os
import sqlite3
import sys

TZ = datetime.timezone(datetime.timedelta(hours=8))
# 學生個人時間線要列出的事件（有 student_pk 的那幾種）
STUDENT_KINDS = ("checkin_reject", "checkin_error", "checkin_ok", "rebind", "manual_status", "unbind")
# 沒有 student_pk、以裝置或場次為單位的失敗與前端回報
DEVICE_FAIL_KINDS = ("land_expired", "land_closed", "code_bad", "code_no_live", "code_throttle", "whoami", "client",
                     "http_err", "invalid", "me_throttle", "checkin_error", "beacon_dropped")
TEACHER_KINDS = ("manual_status", "unbind", "allow_bind", "extend", "reopen")
OK_STATUS = ("present", "late", "pending")
# 被拒原因的中文說明。工具不 import app（正式站是從標準輸入餵進容器），所以抄一份 app.py 的 REJECT_REASON_LABEL；
# tests/test_session_report.py 會對照兩邊的鍵與字句，app 改了這裡沒跟著改測試會紅。
REJECT_REASON_ZH = {
    "pin_format": "PIN 格式", "no_cookie": "沒有裝置識別", "throttle": "太頻繁",
    "grant_expired": "填表逾時", "grant_other_device": "別支手機的表單", "closed": "點名已結束",
    "not_match": "學號或姓名不符", "pin_locked": "PIN 鎖定", "bind_closed": "不開放首次綁定",
    "bind_revoked": "判不在後又想綁",
    "pin_mismatch2": "兩次 PIN 不同", "pin_wrong": "PIN 錯誤", "pin_exists": "已設過 PIN",
    "already_recorded": "本場已有紀錄（打對 PIN）",
    "bound_other_device": "PIN 對但綁在別支（10/17 以前的舊紀錄）",
}
# 旗標彙總要列的幾種（簽到列 flags 欄裡的旗標名；別的旗標如 ip_differs、late_bind 不在這行）。順序固定、沒有也印 0
FLAG_SUMMARY_KEYS = ("same_device", "device_bound_other", "name_variant", "proxy_for", "spot_absent", "qr_after_manual", "same_fp")
STATUS_ZH = {"present": "出席", "late": "遲到", "pending": "待確認", "absent": "缺席", "excused": "請假"}


def db_path() -> str:
    return os.environ.get("ATTEND_DB") or "/data/attend.db"


def hm(ts):
    return datetime.datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M:%S") if ts else "-"


def rel(ts, t0):
    """相對開場的時間，mm:ss；開場前用負號。"""
    d = int(round(ts - t0))
    sign = "-" if d < 0 else ""
    d = abs(d)
    return f"{sign}{d // 60:02d}:{d % 60:02d}"


class Alias:
    def __init__(self):
        self.m: dict[int, str] = {}

    def __call__(self, pk):
        if pk is None:
            return "—"
        if pk not in self.m:
            self.m[pk] = "S%02d" % (len(self.m) + 1)
        return self.m[pk]


def dev4(d8: str) -> str:
    return (d8 or "")[:4] or "----"


def short_reason(kind: str, reason: str) -> str:
    if kind == "client":
        return (reason or "")[:60]
    if kind == "rebind":
        return f"舊裝置 {dev4(reason)}"
    return reason or ""


def pick_sessions(con, argv):
    if "--last" in argv:
        n = int(argv[argv.index("--last") + 1])
        return [r["id"] for r in con.execute("SELECT id FROM sessions ORDER BY opened_at DESC LIMIT ?", (n,))][::-1]
    ids = [int(a) for a in argv if a.isdigit()]
    if ids:
        return ids
    return [r["id"] for r in con.execute("SELECT id FROM sessions ORDER BY opened_at DESC LIMIT 3")][::-1]


def report(con, sid: int, alias: Alias, out) -> None:
    s = con.execute("SELECT s.*, c.code FROM sessions s JOIN courses c ON c.id=s.course_id WHERE s.id=?", (sid,)).fetchone()
    if not s:
        print(f"=== 場次 {sid}：找不到", file=out)
        return
    t0 = s["opened_at"]
    kind = "綁定場次" if s["kind"] == "bind" else "一般點名"
    void = "（已作廢）" if s["voided_at"] else ""
    print(f"=== 場次 {sid}｜{s['code']}｜{kind}{void}", file=out)
    print(f"開場 {hm(t0)}｜出席窗到 {rel(s['open_until'], t0)}｜遲到上限 {rel(s['late_until'], t0)}｜"
          f"結束 {rel(s['closed_at'], t0) if s['closed_at'] else '（未結束）'}", file=out)

    rows = con.execute("SELECT student_pk, status, source, ts, flags, device_hash FROM checkins WHERE session_id=?", (sid,)).fetchall()
    final = {r["student_pk"]: r for r in rows}
    tally = collections.Counter((r["status"], r["source"]) for r in rows)
    print("結果：" + "、".join(f"{STATUS_ZH.get(st, st)}/{src} {n}" for (st, src), n in sorted(tally.items())), file=out)

    end = s["closed_at"] or s["late_until"]
    # 「刪除此次」後重開會沿用同一個場次號，刪除前留下的舊事件不能算進新場——底線同 app 的 compute_funnel
    evs = con.execute("SELECT * FROM events WHERE session_id=? AND ts>=? ORDER BY ts, id", (sid, t0)).fetchall()
    # 沒對到場次的事件（/c 沒有進行中的點名、代碼錯）：取開場前 10 分鐘到結束後 10 分鐘
    loose = con.execute("SELECT * FROM events WHERE session_id IS NULL AND ts BETWEEN ? AND ? ORDER BY ts, id",
                        (t0 - 600, end + 600)).fetchall()

    by_kind = collections.Counter(e["kind"] for e in evs)
    print("事件數：" + "、".join(f"{k} {n}" for k, n in sorted(by_kind.items())), file=out)
    rej = collections.Counter(e["reason"] for e in evs if e["kind"] == "checkin_reject")
    if rej:
        print("被拒原因：" + "、".join(f"{k}（{REJECT_REASON_ZH[k]}）{n}" if k in REJECT_REASON_ZH else f"{k} {n}" for k, n in rej.most_common()), file=out)

    # 本場簽到列的旗標彙總（每列每種旗標算一筆），在逐人時間線之前
    flag_n = collections.Counter(f for r in rows if r["flags"] for f in set(json.loads(r["flags"])))
    print("旗標彙總：" + "、".join(f"{k} {flag_n.get(k, 0)}" for k in FLAG_SUMMARY_KEYS), file=out)

    # 1. 有被擋、出錯、改綁、老師處理過的學生，逐人時間線
    per = collections.defaultdict(list)
    for e in evs:
        if e["student_pk"] is not None and e["kind"] in STUDENT_KINDS:
            per[e["student_pk"]].append(e)
    trouble = [pk for pk, es in per.items() if any(e["kind"] != "checkin_ok" for e in es)]
    print(f"\n-- 有狀況的學生（{len(trouble)} 人）", file=out)
    for pk in sorted(trouble, key=lambda p: per[p][0]["ts"]):
        k = final.get(pk)
        res = f"{STATUS_ZH.get(k['status'], k['status'])}/{k['source']}" if k else "（無紀錄）"
        flags = ",".join(json.loads(k["flags"])) if k and k["flags"] else ""
        print(f"{alias(pk)}  最後：{res}{('  旗標 ' + flags) if flags else ''}", file=out)
        for e in per[pk]:
            print(f"    {rel(e['ts'], t0)}  {e['kind']:<14} {short_reason(e['kind'], e['reason']):<22} 裝置 {dev4(e['device8'])} {e['ua_class']}", file=out)

    # 1b. 每位有 PIN 計次事件的學生，依時間列出「第幾次/上限」序列（dup＝沿用去重沒累加、variant＝姓名只是同姓同字數）
    pinc = collections.defaultdict(list)
    for e in evs:
        if e["kind"] == "pin_count" and e["student_pk"] is not None:
            pinc[e["student_pk"]].append(e["reason"])
    for pk in sorted(pinc, key=lambda p: next(e["ts"] for e in evs if e["kind"] == "pin_count" and e["student_pk"] == p)):
        print(f"{alias(pk)} PIN 計次：" + "、".join(pinc[pk]), file=out)

    # 2. 被擋過、最後卻是自動缺席（結案後跟沒來的人長得一樣，整批更正前要先看這份）
    tried_absent = [pk for pk in per if final.get(pk) is not None and final[pk]["source"] == "auto"
                    and any(e["kind"] in ("checkin_reject", "checkin_error") for e in per[pk])]
    print(f"\n-- 被擋過但最後是自動缺席：{len(tried_absent)} 人 {' '.join(alias(p) for p in tried_absent)}", file=out)

    # 2b. 掃到了、系統認得是誰，卻沒有簽到成功（結束後才掃到、憑證過期、被限流等都會落在這裡）
    seen: dict[int, list[float]] = collections.defaultdict(list)
    for e in evs:
        if e["kind"] == "whoami" and e["reason"] == "known" and e["student_pk"] is not None:
            seen[e["student_pk"]].append(e["ts"])
    land_by_dev: dict[str, list[float]] = collections.defaultdict(list)
    for e in evs:
        if e["kind"] in ("land_ok", "land_closed") and e["device8"]:
            land_by_dev[e["device8"]].append(e["ts"])
    for stu in con.execute("SELECT id, device_hash FROM students WHERE course_id=? AND device_hash IS NOT NULL", (s["course_id"],)):
        seen[stu["id"]].extend(land_by_dev.get(stu["device_hash"][:8], []))
    came = [pk for pk, ts in seen.items() if ts and not (final.get(pk) is not None and final[pk]["status"] in OK_STATUS)]
    came.sort(key=lambda p: min(seen[p]))
    print(f"\n-- 掃到了（認得是誰）卻沒有簽到成功：{len(came)} 人", file=out)
    for pk in came:
        k = final.get(pk)
        res = f"{STATUS_ZH.get(k['status'], k['status'])}/{k['source']}" if k else "（無紀錄）"
        print(f"    {alias(pk)}  最後：{res}  第一次掃到 {rel(min(seen[pk]), t0)}  掃到 {len(seen[pk])} 次", file=out)

    # 3. 老師操作時間線（manual_status 事件較晚才加入；舊場次的手動補登只看得到結果、沒有時間）
    teacher = [e for e in evs if e["kind"] in TEACHER_KINDS]
    manual_no_time = [r["student_pk"] for r in rows if r["source"] == "manual"
                      and not any(e["kind"] == "manual_status" and e["student_pk"] == r["student_pk"] for e in evs)]
    # 沒有進行中場次時按的解除綁定／重設，事件沒有場次號；「當天被解綁可以直接重綁」看的就是這些，
    # 所以列出開場當天 00:00 起、同一門課學生的這類事件。首次綁定開關的事件沒有學號、對不到課程，不列。
    day0 = datetime.datetime.fromtimestamp(t0, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    outside = con.execute(
        "SELECT e.* FROM events e JOIN students st ON st.id=e.student_pk WHERE e.session_id IS NULL AND e.kind='unbind' "
        "AND st.course_id=? AND e.ts BETWEEN ? AND ?", (s["course_id"], day0, end + 600)).fetchall()
    # 「刪除此次」會把場次連同事件整筆刪掉，只留一筆 session_deleted（沒有場次號、reason 帶課程 id）
    deleted = con.execute(
        "SELECT * FROM events WHERE session_id IS NULL AND kind='session_deleted' AND reason LIKE ? AND ts BETWEEN ? AND ?",
        (f"course:{s['course_id']} %", day0, end + 600)).fetchall()
    teacher = sorted([(e, "") for e in teacher] + [(e, "（場次外）") for e in list(outside) + list(deleted)], key=lambda x: (x[0]["ts"], x[0]["id"]))
    print(f"\n-- 老師操作（{len(teacher)} 筆）", file=out)
    for e, tag in teacher:
        print(f"    {rel(e['ts'], t0)}  {e['kind']:<14} {e['reason']:<16} {alias(e['student_pk']) if e['student_pk'] is not None else ''}{tag}", file=out)
    if manual_no_time:
        print(f"    手動列沒有操作時間（舊資料或事件寫入失敗）：{len(manual_no_time)} 筆", file=out)

    # 4. 以裝置為單位的失敗與前端回報（沒有 student_pk 的那些）
    print("\n-- 裝置層的失敗與前端回報", file=out)
    dev_fail = collections.Counter()
    for e in list(evs) + list(loose):
        if e["kind"] == "checkin_error" and e["student_pk"] is not None:
            continue  # 已歸戶的伺服器錯誤在上面「有狀況的學生」逐人列過了
        if e["kind"] in DEVICE_FAIL_KINDS or (e["kind"] == "checkin_reject" and e["student_pk"] is None):
            if e["kind"] == "whoami" and e["reason"] == "known":
                continue
            dev_fail[(e["kind"], short_reason(e["kind"], e["reason"]))] += 1
    if not dev_fail:
        print("    （無）", file=out)
    for (k, r), n in sorted(dev_fail.items()):
        print(f"    {k:<14} {r:<40} {n}", file=out)
    print("", file=out)


def main(argv) -> int:
    path = db_path()
    if not os.path.exists(path):
        print(f"找不到資料庫：{path}", file=sys.stderr)
        return 2
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    alias = Alias()
    for sid in pick_sessions(con, argv):
        report(con, sid, alias, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
