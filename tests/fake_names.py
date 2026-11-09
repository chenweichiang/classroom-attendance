"""偵測規則（不依賴 pytest）：tests/test_fake_names.py 與匯出腳本共用這一份，
規則只寫在這裡。說明見 test_fake_names.py 檔頭。"""
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FAKE = TESTS / "FAKE_NAMES.txt"
TEXT_SUFFIXES = {".py", ".sh", ".md", ".txt", ".csv", ".json", ".html", ".js"}

# 常見姓氏（百家姓前段，含涂／塗、複姓首字）；只用來把「像人名的字串」跟一般中文詞分開
SURNAMES = set(
    "王李張劉陳楊黃趙吳周徐孫馬朱胡郭何林高羅鄭梁謝宋唐許韓馮鄧曹彭曾蕭田董潘袁蔡蔣余于杜葉程魏蘇呂丁任盧姚沈鍾姜崔譚陸"
    "范汪廖石金韋賈夏付方鄒熊白孟秦邱侯江尹薛閻段雷龍黎史陶賀顧毛郝龔邵萬錢嚴賴覃洪武莫孔湯向常溫康施文牛樊葛邢安齊易喬伍"
    "龐顏倪莊聶章魯嶽翟殷詹申歐耿關蘭焦俞左柳甘祝包寧尚符舒阮柯紀梅童凌畢單季裴霍塗涂傅")
HAN = "一-鿿"
QUOTED = re.compile(rf"""["'`]([{HAN}]{{2,4}})["'`]""")
NAME = rf"[{HAN}]{{2,4}}"
LISTED = re.compile(rf"(?<![{HAN}])({NAME}(?:、{NAME})+)(?![{HAN}])")
CSV_ROW = re.compile(rf"^[0-9A-Za-z]*\d[0-9A-Za-z]*,({NAME}),")


def _allowed() -> set:
    return {l.strip() for l in FAKE.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}


def _candidates(line: str):
    for m in QUOTED.finditer(line):
        if m.group(1)[0] in SURNAMES:
            yield m.group(1)
    for m in LISTED.finditer(line):
        parts = m.group(1).split("、")
        if sum(p[0] in SURNAMES for p in parts) >= 2:  # 至少兩個姓氏開頭才當姓名清單，一般詞組的頓號並列不算
            yield from (p for p in parts if p[0] in SURNAMES)
    m = CSV_ROW.match(line)
    if m:
        yield m.group(1)


def scan(paths=None):
    """回傳 [(檔名, 行號, 字串)]：看起來像中文全名、卻不在清單裡的。"""
    ok = _allowed()
    bad = []
    for p in sorted(paths if paths is not None else TESTS.rglob("*")):
        if (not p.is_file() or p.suffix not in TEXT_SUFFIXES or p == FAKE or "__pycache__" in p.parts):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            bad += [(p.name if TESTS not in p.parents else str(p.relative_to(TESTS)), i, w) for w in _candidates(line) if w not in ok]
    return bad
