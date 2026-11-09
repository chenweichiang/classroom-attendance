"""manage.py（名冊匯入與設定 CLI）的回歸：改名留痕、學號正規化、設定值讀回。
每個測試用自己 tmp_path 底下的資料庫與假名冊（姓名都是「測試」開頭的佔位名），不碰 data/rosters/ 的真實名冊。
一律走子行程，測的是使用者在伺服器終端真的會看到的輸出與離開碼。"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cli(tmp_path):
    db = tmp_path / "m.db"
    env = {**os.environ, "ATTEND_DB": str(db)}

    def run(*args):
        return subprocess.run([sys.executable, "-B", str(ROOT / "manage.py"), *args], capture_output=True, text=True,
                              env=env, cwd=ROOT, timeout=60)

    def roster(text, name="roster.csv"):
        p = tmp_path / name
        p.write_text("student_id,name,class\n" + text, encoding="utf-8")
        return str(p)

    def imp(csv_path, *extra, code="M"):
        return run("import-roster", "--code", code, "--name", "課", "--csv", csv_path, *extra)

    def rows():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return {r["student_id"]: dict(r) for r in con.execute("SELECT * FROM students ORDER BY student_id")}
        finally:
            con.close()

    def sql(q, *a):
        con = sqlite3.connect(db)
        con.execute(q, a)
        con.commit()
        con.close()

    run.roster, run.imp, run.rows, run.sql, run.db = roster, imp, rows, sql, db
    return run


def _seed(cli):
    r = cli.imp(cli.roster("9994A001,測試甲乙丙,一甲\n9994A002,測試丁,一甲\n9994A003,測試戊,一甲\n"))
    assert r.returncode == 0, r.stdout + r.stderr


# 改名留痕

def test_rename_is_printed_for_dry_run_and_real_import_only_changed_rows(cli):
    _seed(cli)
    new = cli.roster("9994A001,測試甲乙丙,一甲\n9994A002,測試丁改,二乙\n9994A003,測試戊,一甲\n", "new.csv")
    dry = cli.imp(new, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "9994A002｜測試丁 → 測試丁改" in dry.stdout and "9994A002｜一甲 → 二乙" in dry.stdout, dry.stdout
    assert "9994A001" not in dry.stdout and "9994A003" not in dry.stdout, "沒有變的列不印：" + dry.stdout
    assert cli.rows()["9994A002"]["name"] == "測試丁", "dry-run 不寫"
    real = cli.imp(new)
    assert real.returncode == 0, real.stdout + real.stderr
    assert "9994A002｜測試丁 → 測試丁改" in real.stdout and "9994A001" not in real.stdout, real.stdout
    assert cli.rows()["9994A002"]["name"] == "測試丁改"


def test_rename_of_bound_or_pin_set_student_needs_force_and_changes_nothing_without_it(cli):
    _seed(cli)
    cli.sql("UPDATE students SET device_hash='dh', bound_at=1 WHERE student_id='9994A002'")   # 已綁定
    cli.sql("UPDATE students SET pin_hash='ph', pin_salt='ps' WHERE student_id='9994A003'")   # 已設 PIN
    new = cli.roster("9994A001,測試甲乙丙新,一甲\n9994A002,測試丁改,一甲\n9994A003,測試戊改,一甲\n", "new.csv")
    before = cli.rows()
    for extra in ((), ("--dry-run",)):
        r = cli.imp(new, *extra)
        assert r.returncode == 1 and "--force" in r.stdout + r.stderr, (extra, r.stdout, r.stderr)
        assert "9994A002" in r.stdout + r.stderr and "9994A003" in r.stdout + r.stderr
        assert cli.rows() == before, "要求 --force 時整份匯入都不動（連沒綁定的那位改名也不做）"
    ok = cli.imp(new, "--force")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    after = cli.rows()
    assert [after[s]["name"] for s in ("9994A001", "9994A002", "9994A003")] == ["測試甲乙丙新", "測試丁改", "測試戊改"]
    assert after["9994A002"]["device_hash"] == "dh" and after["9994A003"]["pin_hash"] == "ph", "改名不動綁定與 PIN"


def test_rename_of_unbound_student_without_pin_needs_no_force(cli):
    _seed(cli)
    r = cli.imp(cli.roster("9994A001,測試甲乙丙改,一甲\n9994A002,測試丁,一甲\n9994A003,測試戊,一甲\n", "n.csv"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_truncated_new_name_is_refused_even_with_force(cli):
    _seed(cli)
    before = cli.rows()
    new = cli.roster("9994A001,測試甲,一甲\n9994A002,測試丁,一甲\n9994A003,測試戊,一甲\n", "n.csv")  # 舊名的前綴、較短
    for extra in ((), ("--force",), ("--dry-run",)):
        r = cli.imp(new, *extra)
        assert r.returncode == 1 and "截斷" in r.stdout + r.stderr and "9994A001" in r.stdout + r.stderr, (extra, r.stdout, r.stderr)
        assert cli.rows() == before
    # 較短但不是前綴（真的改名）不算截斷
    ok = cli.imp(cli.roster("9994A001,測試乙,一甲\n9994A002,測試丁,一甲\n9994A003,測試戊,一甲\n", "n2.csv"))
    assert ok.returncode == 0, ok.stdout + ok.stderr


# 學號正規化

def test_ids_are_normalized_like_checkin_and_reimport_updates_instead_of_duplicating(cli):
    _seed(cli)
    # 全形英數、小寫、前後空白、夾零寬字元：都要落到同一個學號上
    messy = "\ufeff ９９９４ａ００１\u200b ,測試甲乙丙新,一甲"
    r = cli.imp(cli.roster(messy + "\n9994A002,測試丁,一甲\n9994A003,測試戊,一甲\n", "m.csv"))
    assert r.returncode == 0, r.stdout + r.stderr
    rows = cli.rows()
    assert sorted(rows) == ["9994A001", "9994A002", "9994A003"], sorted(rows)
    assert rows["9994A001"]["name"] == "測試甲乙丙新", "更新到原本那列，不是新增一列"
    assert "新增 0、更新 3" in r.stdout, r.stdout


def test_id_with_illegal_characters_blocks_the_whole_import(cli):
    bad = cli.roster("9994A001,測試甲,一甲\nS-02,測試乙,一甲\n11 54,測試丙,一甲\n", "b.csv")
    r = cli.imp(bad)
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "第 3 列" in out and "第 4 列" in out and "[0-9A-Z]" in out, out
    assert not cli.db.exists() or cli.rows() == {}, "不合格的學號出現時，整份都不寫"


def test_prune_with_same_name_in_gone_and_new_requires_force_and_lists_id_pairs_only(cli):
    _seed(cli)
    # 9994A002 不在新名冊、新名冊多了同名的 9994A099（多半是學號打錯或換號），一律要 --force
    new = cli.roster("9994A001,測試甲乙丙,一甲\n9994A099,測試丁,一甲\n9994A003,測試戊,一甲\n", "p.csv")
    before = cli.rows()
    for extra in ((), ("--dry-run",)):
        r = cli.imp(new, "--prune", *extra)
        out = r.stdout + r.stderr
        assert r.returncode == 1 and "--force" in out, (extra, out)
        assert "9994A002" in out and "9994A099" in out, out
        assert "測試丁" not in out, "配對只列學號，不列姓名：" + out
        assert cli.rows() == before
    ok = cli.imp(new, "--prune", "--force")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert sorted(cli.rows()) == ["9994A001", "9994A003", "9994A099"]


# set 印出的值要等於實際生效值

@pytest.mark.parametrize("key,value,effective,range_text", [
    ("spot_n", "0", 1, "1"),
    ("pin_max_fail", "0", 1, "1"),
    ("open_minutes", "0", 1, "1"),
    ("grant_seconds", "5", 30, "30"),
    ("grant_seconds", "5000", 900, "900"),
])
def test_set_value_that_would_be_clamped_is_refused_with_effective_value(cli, key, value, effective, range_text):
    _seed(cli)
    r = cli("set", "--code", "M", "--key", key, "--value", value)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert f"實際生效 {effective}" in out and "允許範圍" in out and range_text in out, out
    assert f"M.{key} = " not in r.stdout, "不可印出沒有生效的值當作成功"
    con = sqlite3.connect(cli.db)
    raw = json.loads(con.execute("SELECT settings FROM courses WHERE code='M'").fetchone()[0])
    con.close()
    assert key not in raw, "被拒絕的值不寫進資料庫"


def test_legal_value_still_set_and_printed(cli):
    _seed(cli)
    r = cli("set", "--code", "M", "--key", "spot_n", "--value", "4")
    assert r.returncode == 0 and "M.spot_n = 4" in r.stdout, r.stdout + r.stderr
    r = cli("set", "--code", "M", "--key", "grant_seconds", "--value", "30")
    assert r.returncode == 0 and "M.grant_seconds = 30" in r.stdout, r.stdout + r.stderr


# 名冊匯錯課程代碼

def _seed_three_students(cli):
    r = cli.imp(cli.roster("9994A001,測試1,甲班\n9994A002,測試2,甲班\n9994A003,測試3,甲班\n"))
    assert r.returncode == 0, r.stdout + r.stderr
    return cli.rows()


def _course_name(cli):
    con = sqlite3.connect(cli.db)
    try:
        return con.execute("SELECT name FROM courses WHERE code='M'").fetchone()[0]
    finally:
        con.close()


def test_zero_overlap_with_existing_roster_is_refused_without_force(cli):
    before = _seed_three_students(cli)
    other = cli.roster("9994A101,測試11,乙班\n9994A102,測試12,乙班\n", "other.csv")
    for extra in ((), ("--dry-run",)):
        r = cli.imp(other, *extra)
        out = r.stdout + r.stderr
        assert r.returncode == 1, (extra, out)
        assert "CSV 的 2 人與現有名冊 3 人沒有任何一個學號相同，可能匯錯課程代碼" in out, out
        assert cli.rows() == before, "擋下時資料庫不動"
    ok = cli.imp(other, "--force")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert {"9994A101", "9994A102"} <= set(cli.rows())


def test_partial_overlap_and_first_import_are_unaffected(cli):
    _seed_three_students(cli)  # 課程不存在的第一次匯入本身就是「不受影響」的證明
    r = cli.imp(cli.roster("9994A003,測試3,甲班\n9994A104,測試14,甲班\n", "part.csv"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "沒有任何一個學號相同" not in r.stdout + r.stderr


def test_changed_course_name_is_refused_without_force_and_renamed_with_it(cli):
    before = _seed_three_students(cli)
    same = cli.roster("9994A001,測試1,甲班\n9994A002,測試2,甲班\n9994A003,測試3,甲班\n", "same.csv")
    for extra in ((), ("--dry-run",)):
        r = cli("import-roster", "--code", "M", "--name", "新課名", "--csv", same, *extra)
        out = r.stdout + r.stderr
        assert r.returncode == 1, (extra, out)
        assert "課名將由「課」改成「新課名」" in out, out
        assert _course_name(cli) == "課" and cli.rows() == before
    ok = cli("import-roster", "--code", "M", "--name", "新課名", "--csv", same, "--force")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert _course_name(cli) == "新課名"


def test_same_course_name_prints_no_rename_notice(cli):
    _seed_three_students(cli)
    r = cli.imp(cli.roster("9994A001,測試1,甲班\n9994A002,測試2,甲班\n9994A003,測試3,甲班\n", "same.csv"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "課名將由" not in r.stdout + r.stderr
