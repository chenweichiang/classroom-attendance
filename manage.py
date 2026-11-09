"""名冊匯入與課程管理 CLI。用法：
  python manage.py import-roster --code NTUB-CD --name "脈絡設計與實踐" --csv roster.csv
  python manage.py list
匯入可重複執行：同學號更新姓名與班級，不動 PIN 與裝置綁定；名冊裡沒有的舊學號保留（要刪用 --prune；
先用 --dry-run 看會新增/更新/刪除幾人，--prune 要刪的人數過多會先擋下，確定要刪再加 --force）。
匯入（含 --dry-run）會逐列印出真的有變的欄位（學號｜舊姓名 → 新姓名；班級同理），印在執行的終端、不寫檔；
已綁定或已設 PIN 的人姓名有變要加 --force；新姓名是舊姓名的前綴而且比較短（疑似截斷）一律擋下，--force 也不放行。
學號一律先正規化（全形轉半形、去零寬字元與空白、轉大寫），不是英數字的整份擋下。
"""
import argparse, csv, re, sys
import app as A

# set 寫入的值會被 app.course_settings 夾回範圍時，拒絕並說明實際生效值；範圍文字照 course_settings 的夾法
SETTING_RANGES = {
    "open_minutes": "1 以上", "late_minutes": "不小於 open_minutes", "qr_interval": "5 以上", "qr_grace": "0 到 120",
    "spot_n": "1 以上", "grant_seconds": "30 到 900", "pin_max_fail": "1 以上", "bind_minutes": "3 到 30",
}


def normalize_student_id(raw: str) -> str:
    """正規化規則的正本在 app.py（簽到查學號同一支），這裡只是轉呼叫。"""
    return A.normalize_student_id(raw)


def import_roster(a):
    """先整份讀完、檢查完，才開始寫資料庫——任何一項檢查不過就離開碼 1，資料庫完全不動
    （連姓名更新也不做）。`--dry-run` 走同一套檢查，只是最後印計畫不寫；`--force` 只放行「要刪的
    人數過多」「已綁定或已設 PIN 的人改名」「要刪的人與要新增的人同名」這幾項，「有列被跳過」「學號不合格」
    「疑似截斷的新姓名」永遠不能用 --force 放行，只能先修 CSV。"""
    force = getattr(a, "force", False)
    dry_run = getattr(a, "dry_run", False)
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    if not rows:
        sys.exit("CSV 是空的")
    if not any(k in fieldnames for k in ("student_id", "學號")):
        sys.exit("CSV 表頭找不到學號欄（student_id 或 學號），沒有做任何變更")

    def pick(r, *keys):
        for k in keys:
            if k in r and r[k].strip():
                return r[k].strip()
        return ""

    with A.db() as con:
        # 讀現況（唯讀）：課程可能還不存在（第一次匯入），這裡不建立它，確定要寫才建
        crow = con.execute("SELECT id, name FROM courses WHERE code=?", (a.code,)).fetchone()
        cid = crow["id"] if crow else None
        existing = {r["student_id"]: dict(r) for r in con.execute(
            "SELECT id, student_id, name, cls, device_hash, pin_hash FROM students WHERE course_id=?", (cid,))} if cid is not None else {}
        had_sessions = bool(cid is not None and con.execute(
            "SELECT 1 FROM sessions WHERE course_id=? LIMIT 1", (cid,)).fetchone())

        seen = set()
        to_insert = []  # (student_id, name, cls)
        to_update = []  # (existing_pk, student_id, name, cls)
        skipped_no_name = []  # 學號有值、姓名空白的列號（第 1 列是表頭，資料列從 2 起算）
        bad_ids = []  # 正規化後不是 [0-9A-Z]+ 的 (列號, 學號)
        n_skipped_no_sid = 0
        for i, r in enumerate(rows, start=2):
            sid = normalize_student_id(pick(r, "student_id", "學號"))
            if sid and not re.fullmatch(r"[0-9A-Z]+", sid):
                bad_ids.append((i, sid))
                continue
            name = pick(r, "name", "姓名")
            cls = pick(r, "class", "班級", "cls")
            if not sid:
                n_skipped_no_sid += 1
                continue
            if not name:
                # 學號照樣算進「名冊有這個人」，但不更新姓名——不確定新姓名是什麼，不能亂寫
                seen.add(sid)
                skipped_no_name.append(i)
                continue
            seen.add(sid)
            ex = existing.get(sid)
            if ex:
                to_update.append((ex["id"], sid, name, cls))
            else:
                to_insert.append((sid, name, cls))

        gone = [s for ssid, s in existing.items() if ssid not in seen]

        for i in skipped_no_name:
            print(f"姓名空白：第 {i} 列")
        if bad_ids:  # 學號不合格，整份不寫（--force 也不放行）
            for i, bad in bad_ids:
                print(f"學號不合格：第 {i} 列 {bad!r}（正規化後要全是 [0-9A-Z]）")
            sys.exit(f"CSV 有 {len(bad_ids)} 列學號不是英數字（見上面列出的列號），先修正 CSV；這個原因不能用 --force 略過")

        # 逐列印出真的有變的欄位（在這個終端印，不寫檔），再做改名的檢查
        renamed_bound = []
        truncated = []
        for eid, sid, name, cls in to_update:
            ex = next(e for e in existing.values() if e["id"] == eid)
            if ex["name"] != name:
                print(f"改名：{sid}｜{ex['name']} → {name}")
                if len(name) < len(ex["name"]) and ex["name"].startswith(name):
                    truncated.append(sid)
                if ex["device_hash"] or ex["pin_hash"]:
                    renamed_bound.append(sid)
            if (ex["cls"] or "") != cls:
                print(f"改班級：{sid}｜{ex['cls'] or ''} → {cls}")
        if truncated:
            sys.exit(f"新姓名是舊姓名的前綴而且比較短（疑似被截斷）：{', '.join(truncated)}。先確認匯出檔沒有截字再重匯；"
                     "這個原因不能用 --force 略過")
        if renamed_bound and not force:
            sys.exit(f"已綁定或已設 PIN 的學生姓名有變：{', '.join(renamed_bound)}。確定是正式改名（不是匯出檔的字被截掉或寫錯）"
                     "再加 --force")

        # 課程代碼已存在時的兩種「多半是打錯代碼」——CSV 與現有名冊學號零重疊、或 --name 與現有課名不同。
        # 沒有 --force 就整份不動、離開碼 1（--dry-run 走同一套）。
        guard = []
        if crow is not None and existing and not (seen & set(existing)):
            guard.append(f"CSV 的 {len(seen)} 人與現有名冊 {len(existing)} 人沒有任何一個學號相同，可能匯錯課程代碼")
        if crow is not None and crow["name"] != a.name:
            guard.append(f"課名將由「{crow['name']}」改成「{a.name}」")
        for g in guard:
            print(g)
        if guard and not force:
            sys.exit("上面的提醒要確認過、確定沒有匯錯，再加 --force")

        if a.prune:
            skipped_total = n_skipped_no_sid + len(skipped_no_name)
            if skipped_total:
                sys.exit(f"CSV 有 {skipped_total} 列學號或姓名空白（見上面列出的列號），先修正 CSV 再 --prune；"
                         "這個原因不能用 --force 略過")
            # 要刪的人與要新增的人姓名相同，多半是學號打錯或換號（簽到紀錄會跟著舊學號一起被刪）：一律要 --force，列出配對（只列學號）
            ins_by_name = {}
            for isid, iname, _icls in to_insert:
                ins_by_name.setdefault(iname, []).append(isid)
            pairs = [(g["student_id"], nsid) for g in gone for nsid in ins_by_name.get(g["name"], [])]
            if pairs and not force:
                sys.exit("--prune 要刪的人與要新增的人姓名相同（舊學號 → 新學號）："
                         + "、".join(f"{o} → {n}" for o, n in pairs) + "。確定是換學號再加 --force")
            total_current = len(existing)
            if len(gone) > 3 and total_current > 0 and len(gone) > total_current * 0.1 and not force:
                sys.exit(f"這次 --prune 要刪 {len(gone)} 人，超過目前名冊 {total_current} 人的一成，"
                         "先用 --dry-run 確認名單，確定要刪再加 --force")

        if dry_run:
            print(f"會新增 {len(to_insert)}、更新 {len(to_update)}、刪除 {len(gone) if a.prune else 0}")
            if a.prune:
                for g in gone:
                    n = con.execute("SELECT COUNT(*) FROM checkins WHERE student_pk=?", (g["id"],)).fetchone()[0]
                    print(f"  將刪除 {g['student_id']}（{n} 筆簽到）")
            return

        con.execute("INSERT INTO courses(code, name) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                    (a.code, a.name))
        cid = con.execute("SELECT id FROM courses WHERE code=?", (a.code,)).fetchone()["id"]
        for eid, _sid, name, cls in to_update:
            con.execute("UPDATE students SET name=?, cls=? WHERE id=?", (name, cls, eid))
        for sid, name, cls in to_insert:
            # 首次匯入（課程還沒點過名）視為學期初 0；之後加選的人記匯入時間，早於此的場次不算他缺席
            con.execute("INSERT INTO students(course_id, student_id, name, cls, created_at) VALUES(?,?,?,?,?)",
                        (cid, sid, name, cls, A.now() if had_sessions else 0))
        if a.prune and gone:
            for g in gone:  # 連同他的簽到、抽點、PIN 計數、事件記錄一起刪，不留孤兒列（投影幕才不會出現 3 / 2）
                for t in ("checkins", "spotchecks", "pin_fail", "events"):
                    con.execute(f"DELETE FROM {t} WHERE student_pk=?", (g["id"],))
                con.execute("DELETE FROM students WHERE id=?", (g["id"],))
            print("刪除", len(gone), "人（含其簽到紀錄）")
    print(f"課程 {a.code}（{a.name}）：新增 {len(to_insert)}、更新 {len(to_update)}")


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
        # 寫之前先用 course_settings 讀回，值會被夾成別的數字就拒絕（印出去的值必須等於實際生效值）
        probe = A.sqlite3.connect(":memory:")
        probe.row_factory = A.sqlite3.Row
        effective = A.course_settings(probe.execute("SELECT ? AS settings", (json.dumps(st),)).fetchone())[a.key]
        probe.close()
        if effective != v:
            sys.exit(f"{a.key} = {v!r} 不會生效：實際生效 {effective!r}（允許範圍 {SETTING_RANGES.get(a.key, '見 course_settings')}），沒有寫入")
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
    q.add_argument("--csv", required=True); q.add_argument("--prune", action="store_true")
    q.add_argument("--force", action="store_true", help="要刪的人數超過 3 人且超過一成時，確定要刪才加這個")
    q.add_argument("--dry-run", action="store_true", dest="dry_run", help="只印會新增/更新/刪除幾人，不寫資料庫")
    q.set_defaults(fn=import_roster)
    q = sp.add_parser("delete-course"); q.add_argument("--code", required=True); q.set_defaults(fn=delete_course)
    q = sp.add_parser("set"); q.add_argument("--code", required=True); q.add_argument("--key", required=True); q.add_argument("--value", required=True); q.set_defaults(fn=set_setting)
    q = sp.add_parser("list"); q.set_defaults(fn=list_courses)
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
