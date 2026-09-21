#!/usr/bin/env python3
"""重做朱雀仿宋的自架子集（static/fonts/ZhuqueFangsong-subset.woff2）。

什麼時候要跑：改了樣板或 app.py 裡會出現在標題（h1、h2）、收據印章、收據姓名的文字，或名冊有新同學。
缺字不會壞，只會退回系統明體，同一行裡兩種字型（2026-09-21 B061：重整新增的「候」章與四個標題字沒進子集）。

這個字型只套在 h1、h2、收據印章與收據姓名（base.html 的 --display），檔案大小學生每次第一次載入都要付，
所以收字收斂在「畫面上可能出現的字」，不收程式註解。

收字來源（取聯集）：
  1. templates/*.html 全文，加上 app.py 的字串常值（狀態名、場次種類、錯誤訊息；不含註解與 docstring）
  2. 名冊 CSV 的姓名與班級（--roster-dir；個資只在本機讀，產物只含字形、不含姓名）
  3. 現有子集已經有的字（不讓舊字掉出去）
  4. --extra 額外指定的字（例如新課名）

原始字型不進版控。取得方式（OFL-1.1，官方 repo）：
  gh release download v0.212 -R TrionesType/zhuque -p "ZhuqueFangsong-v0.212.zip" && unzip ZhuqueFangsong-v0.212.zip

執行（不裝進專案的 .venv，用 uv 的臨時環境）：
  uv run --no-project --with fonttools --with brotli python tools/subset_font.py --source /path/to/ZhuqueFangsong-Regular.ttf
  加 --check 只檢查缺字、不寫檔（缺字時離開碼 1）。
"""
import argparse
import ast
import csv
import pathlib
import sys

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = pathlib.Path(__file__).resolve().parent.parent
OUT = HERE / "static" / "fonts" / "ZhuqueFangsong-subset.woff2"
DEFAULT_ROSTERS = HERE / "data" / "rosters"
BASIC = "".join(chr(c) for c in range(0x20, 0x7F)) + "，。、：；？！「」『』（）《》〈〉—…·％／＋－～　"


def app_string_constants() -> list[str]:
    """app.py 的字串常值，扣掉 docstring（docstring 是給人讀的說明，不會出現在畫面上）。"""
    tree = ast.parse((HERE / "app.py").read_text(encoding="utf-8"))
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                doc_ids.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids]


def is_han(cp: int) -> bool:
    return 0x3400 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2FA1F


def collect(roster_dir: pathlib.Path, extra: str) -> tuple[set[int], dict[str, int], set[int]]:
    chars: set[str] = set(BASIC) | set(extra)
    stat = {}
    src = "".join(p.read_text(encoding="utf-8") for p in sorted((HERE / "templates").glob("*.html")))
    src += "".join(app_string_constants())
    code_chars = {ch for ch in src if ord(ch) > 0x7F and not ch.isspace()}
    stat["樣板與 app.py 字串"] = len(code_chars)
    chars |= code_chars
    roster_chars: set[str] = set()
    if roster_dir.is_dir():
        for f in sorted(roster_dir.glob("*.csv")):
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for row in csv.reader(fh):
                    roster_chars |= {ch for cell in row for ch in cell if ord(ch) > 0x7F and not ch.isspace()}
    stat["名冊"] = len(roster_chars)
    chars |= roster_chars
    cps = {ord(ch) for ch in chars}
    if OUT.exists():
        old = set(TTFont(OUT).getBestCmap().keys())
        stat["現有子集"] = len(old)
        cps |= old
    return cps, stat, {ord(ch) for ch in code_chars}


def main() -> int:
    ap = argparse.ArgumentParser(description="重做朱雀仿宋的自架子集")
    ap.add_argument("--source", type=pathlib.Path, help="ZhuqueFangsong-Regular.ttf 的路徑（--check 時可省略）")
    ap.add_argument("--roster-dir", type=pathlib.Path, default=DEFAULT_ROSTERS)
    ap.add_argument("--extra", default="", help="額外要收的字")
    ap.add_argument("--check", action="store_true", help="只檢查現有子集有沒有缺字，不寫檔")
    a = ap.parse_args()

    want, stat, ui = collect(a.roster_dir, a.extra)
    print("收字來源：" + "、".join(f"{k} {v} 字" for k, v in stat.items()) + f"；聯集 {len(want)} 個碼位")

    if a.check:
        have = set(TTFont(OUT).getBestCmap().keys()) if OUT.exists() else set()
        ui_missing = sorted(c for c in ui - have if is_han(c))
        other = sorted(c for c in want - have if is_han(c) and c not in ui)
        print(f"畫面文字缺 {len(ui_missing)} 個漢字：" + "".join(chr(c) for c in ui_missing[:80]))
        # 名冊的罕用字可能連原始字型都沒有，沒有原始檔分不出來，所以只報數、不判失敗
        print(f"名冊用字不在子集裡的 {len(other)} 個（罕用字原始字型也沒有的話屬正常，會退回系統明體）")
        return 1 if ui_missing else 0

    if not a.source or not a.source.is_file():
        print("要給 --source（原始 ttf），取得方式見檔頭", file=sys.stderr)
        return 2
    full = set(TTFont(a.source).getBestCmap().keys())
    absent_ui = sorted(c for c in (want - full) & ui if is_han(c))
    absent_other = len([c for c in want - full if is_han(c) and c not in ui])
    if absent_ui:
        print(f"畫面文字裡原始字型本身沒有的漢字 {len(absent_ui)} 個（會退回系統明體）：" + "".join(chr(c) for c in absent_ui))
    if absent_other:  # 名冊用字只報數，不把姓名用字印出來
        print(f"名冊用字裡原始字型本身沒有的 {absent_other} 個（會退回系統明體）")
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    font = subset.load_font(str(a.source), opts)
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=sorted(want & full))
    sub.subset(font)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    subset.save_font(font, str(OUT), opts)
    got = set(TTFont(OUT).getBestCmap().keys())
    print(f"已寫入 {OUT.relative_to(HERE)}：{len(got)} 個碼位、{OUT.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
