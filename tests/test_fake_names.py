"""測試素材裡不放真實感的中文全名。

tests/ 底下（含 gate.sh 與彩排生成的 fixtures/roster.csv）出現的中文全名，只准是 tests/FAKE_NAMES.txt 裡的
明顯虛構名字，或「測試＋數字」這種佔位名。清單放 tests/ 而不放 tests/fixtures/：fixtures 整個被 .gitignore 排除，
乾淨 checkout 上找不到清單，這條測試就會失去作用。

偵測範圍（刻意收窄，誤判時調範圍，不是把一般詞加進白名單）：
- 單引號、雙引號或反引號包住的「純漢字 2–4 字」整串，且第一字是常見姓氏；
- 用頓號串起來的姓名清單（說明文字裡列名冊用，至少兩個，每個 2–4 字、姓氏開頭）；
- 名冊 CSV 的姓名欄（第二欄，學號格式開頭的列）。
帶數字的佔位名（學生1、清大5、測試7）不是純漢字，天然不在範圍內；「後加」「加選生」「測試學生」這類不以姓氏開頭的佔位詞
也不在範圍內。
"""
import re

import pytest

from fake_names import FAKE, NAME, scan  # 規則與掃描在 fake_names.py（export_public.py 也用同一份）


def test_fake_names_list_is_well_formed():
    names = [l.strip() for l in FAKE.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert len(names) == len(set(names)), "FAKE_NAMES.txt 有重複"
    for n in names:
        assert re.fullmatch(NAME, n), f"清單裡只放 2–4 個純漢字的名字：{n!r}"


def test_no_realistic_chinese_full_names_in_test_material():
    bad = scan()
    # 只印檔名與行號，不把字串印進輸出（個資）
    if bad:  # 用 pytest.fail 而不是 assert：assert 失敗時 pytest 會把整個清單（含字串）印出來
        pytest.fail("測試素材裡有不在 FAKE_NAMES.txt 的中文全名（檔名:行號）：" + ", ".join(f"{f}:{n}" for f, n, _ in bad), pytrace=False)


def test_detector_catches_and_ignores_the_right_things(tmp_path):
    # 範例名字用空白分隔的一整串再 split：原始碼裡不出現「單獨被引號包住的全名」，這支檔自己才掃得過
    n1, n2, n3, n4 = "周甲乙 鄭丙丁 許戊己 何庚辛".split()  # 明顯虛構、但姓氏開頭的 3 字，偵測規則一樣要抓到
    f = tmp_path / "x.py"
    f.write_text(f'a("9994A001", "{n1}")\nb = ["測試7", "學生1", "清大5", "後加", "加選生", "測試學生"]\n'
                 f'# 名冊：{n2}、{n3}\nfill("#name", "出席")\n', encoding="utf-8")
    c = tmp_path / "r.csv"
    c.write_text(f"student_id,name,class\n9994A001,{n4},班\n9994A002,測試5,班\n", encoding="utf-8")
    assert sorted(w for _, _, w in scan([f, c])) == sorted([n1, n2, n3, n4])
