"""名冊匯入與課程管理 CLI。用法：
  python manage.py import-roster --code NTUB-CD --name "脈絡設計與實踐" --csv roster.csv
  python manage.py list
匯入可重複執行：同學號更新姓名與班級，不動 PIN 與裝置綁定；名冊裡沒有的舊學號保留（要刪用 --prune）。
"""
import argparse, csv, sys
import app as A


def import_roster(a):
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("CSV 是空的")
    def pick(r, *keys):
        for k in keys:
            if k in r and r[k].strip():
                return r[k].strip()
        return ""
    with A.db() as con:
        con.execute("INSERT INTO courses(code, name) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                    (a.code, a.name))
        cid = con.execute("SELECT id FROM courses WHERE code=?", (a.code,)).fetchone()["id"]
        seen = set(); n_new = n_upd = 0
        for r in rows:
            sid = pick(r, "student_id", "學號").upper(); name = pick(r, "name", "姓名"); cls = pick(r, "class", "班級", "cls")
            if not sid or not name:
                continue
            seen.add(sid)
            ex = con.execute("SELECT id FROM students WHERE course_id=? AND student_id=?", (cid, sid)).fetchone()
            if ex:
                con.execute("UPDATE students SET name=?, cls=? WHERE id=?", (name, cls, ex["id"])); n_upd += 1
            else:
                # 首次匯入（課程還沒點過名）視為學期初 0；之後加選的人記匯入時間，早於此的場次不算他缺席
                had_sessions = con.execute("SELECT 1 FROM sessions WHERE course_id=? LIMIT 1", (cid,)).fetchone()
                con.execute("INSERT INTO students(course_id, student_id, name, cls, created_at) VALUES(?,?,?,?,?)",
                            (cid, sid, name, cls, A.now() if had_sessions else 0)); n_new += 1
        if a.prune:
            gone = [r for r in con.execute("SELECT id, student_id FROM students WHERE course_id=?", (cid,)) if r["student_id"] not in seen]
            for g in gone:  # 連同他的簽到、抽點、PIN 計數、事件記錄一起刪，不留孤兒列（投影幕才不會出現 3 / 2）
                for t in ("checkins", "spotchecks", "pin_fail", "events"):
                    con.execute(f"DELETE FROM {t} WHERE student_pk=?", (g["id"],))
                con.execute("DELETE FROM students WHERE id=?", (g["id"],))
            print("刪除", len(gone), "人（含其簽到紀錄）")
    print(f"課程 {a.code}（{a.name}）：新增 {n_new}、更新 {n_upd}")


def delete_course(a):
    with A.db() as con:
        c = con.execute("SELECT id FROM courses WHERE code=?", (a.code,)).fetchone()
        if not c:
            sys.exit("沒有這個課程代碼")
        cid = c["id"]
        sids = [r["id"] for r in con.execute("SELECT id FROM sessions WHERE course_id=?", (cid,))]
        for sid in sids:
            for t in ("checkins", "pin_fail", "spotchecks", "events"):
                con.execute(f"DELETE FROM {t} WHERE session_id=?", (sid,))
        con.execute("DELETE FROM sessions WHERE course_id=?", (cid,))
        con.execute("DELETE FROM students WHERE course_id=?", (cid,))
        con.execute("DELETE FROM courses WHERE id=?", (cid,))
    print(f"已刪課程 {a.code}（含 {len(sids)} 次點名）")


def set_setting(a):
    import json
    with A.db() as con:
        c = con.execute("SELECT * FROM courses WHERE code=?", (a.code,)).fetchone()
        if not c:
            sys.exit("沒有這個課程代碼")
        st = A.course_settings(c)
        types = {"open_minutes": int, "late_minutes": int, "qr_interval": int, "qr_grace": int, "spot_n": int,
                 "grant_seconds": int, "pin_max_fail": int, "bind_minutes": int, "ip_check": bool, "allow_bind": bool,
                 "discord_channel": str}
        if a.key not in types:
            sys.exit(f"沒有這個設定：{a.key}（可用：{', '.join(types)}）")
        v = a.value
        if types[a.key] is bool:
            if v.lower() not in ("true", "false", "1", "0"):
                sys.exit(f"{a.key} 要 true/false")
            v = v.lower() in ("true", "1")
        elif types[a.key] is int:
            if not (v.isascii() and v.isdigit()):
                sys.exit(f"{a.key} 要整數")
            v = int(v)
        elif a.key == "discord_channel" and v and not (v.isascii() and v.isdigit() and 15 <= len(v) <= 22):
            sys.exit("discord_channel 要 17 到 20 位數字或留空")
        st[a.key] = v
        if a.key in ("open_minutes", "late_minutes") and int(st["late_minutes"]) < int(st["open_minutes"]):
            sys.exit(f"late_minutes（{st['late_minutes']}）不能小於 open_minutes（{st['open_minutes']}）")
        if a.key == "qr_interval" and v < 5:
            sys.exit("qr_interval 最少 5 秒")
        if a.key == "qr_grace" and v > 120:
            sys.exit("qr_grace 最多 120 秒")
        if a.key == "bind_minutes" and not (3 <= v <= 30):
            sys.exit("bind_minutes 要在 3 到 30 之間")
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps(st), c["id"]))
    print(f"{a.code}.{a.key} = {v!r}")


def list_courses(a):
    with A.db() as con:
        for c in con.execute("SELECT * FROM courses ORDER BY id"):
            n = con.execute("SELECT COUNT(*) FROM students WHERE course_id=?", (c["id"],)).fetchone()[0]
            b = con.execute("SELECT COUNT(*) FROM students WHERE course_id=? AND device_hash IS NOT NULL", (c["id"],)).fetchone()[0]
            s = con.execute("SELECT COUNT(*) FROM sessions WHERE course_id=?", (c["id"],)).fetchone()[0]
            print(f"[{c['id']}] {c['code']}  {c['name']}  名冊 {n} 人、已綁定 {b}、點名 {s} 次")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("import-roster"); q.add_argument("--code", required=True); q.add_argument("--name", required=True)
    q.add_argument("--csv", required=True); q.add_argument("--prune", action="store_true"); q.set_defaults(fn=import_roster)
    q = sp.add_parser("delete-course"); q.add_argument("--code", required=True); q.set_defaults(fn=delete_course)
    q = sp.add_parser("set"); q.add_argument("--code", required=True); q.add_argument("--key", required=True); q.add_argument("--value", required=True); q.set_defaults(fn=set_setting)
    q = sp.add_parser("list"); q.set_defaults(fn=list_courses)
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
