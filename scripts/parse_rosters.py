"""範例：把教務系統匯出的名冊轉成 data/rosters/*.csv（student_id、name、class 三欄），
再交給 manage.py import-roster。處理的是清大與北商的匯出格式，別的學校照自己的格式改。

  --nthu-csv  清大校務資訊系統匯出的選課名單文字檔（Big5、逗號分隔，姓名完整且帶班別）→ nthu-oop.csv
  --nthu-pdf  只有清大點名單 PDF 時的退路（用 pdftotext 抽字，姓名最多抓三個字）→ nthu-oop.csv
  --ntub-xls  北商 PoolExport.xls（內容其實是 HTML，一個檔可含多門課）；課名對應的輸出檔名寫在 NTUB_FILES
  --cd-csv    已經是 student_id、name、class 欄位的 CSV，轉存成 ntub-contextdesign.csv
              （跟 --ntub-xls 的其中一門寫同一個檔，兩個都給時以 --cd-csv 為準）
  --allow-skip  有格式外的列被跳過時照樣寫出名冊

格式外的列（學號格式不對、姓名空白）不會悄悄消失：每一列印出檔名、列號與原因（不印姓名，學號只印
前兩碼與長度）。有任何一列被跳過就以非零結束，而且一個名冊檔都不寫、舊檔原樣保留；確定要放行才加
--allow-skip，因為之後 import-roster --prune 會把不在名冊裡的人連同簽到紀錄一起刪掉。
北商的檔遇到不認得的「科目名稱」標頭寫法、標頭之前就出現資料列，或 &#NNNNN; 組出 U+FFFD，直接停下。

用法：python3 scripts/parse_rosters.py --nthu-csv STU_LIST.txt --ntub-xls PoolExport.xls
"""
import argparse, csv, html, re, subprocess, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "rosters"
SKIPPED = []  # (檔名, 列號, 原因)


def mask_id(sid):
    """學號只露前兩碼與長度，免得輸出裡出現個資。"""
    return f"{sid[:2]}…（共 {len(sid)} 碼）" if sid else "（空白）"


def skip(path, line, reason):
    SKIPPED.append((Path(path).name, line, reason))


def stop(path, line, reason):
    sys.exit(f"停止：{Path(path).name} 第 {line} 列：{reason}")


PENDING = []  # 解析完才一起寫：中途停下或有跳過列（沒放行）時一個檔都不寫，舊名冊原樣保留


def write(name, rows):
    PENDING.append((name, rows))


def flush():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in PENDING:
        with open(OUT / name, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["student_id", "name", "class"]); w.writerows(rows)
        print(name, len(rows))


def nthu(pdf):
    txt = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
    out, seen = [], set()
    for line in txt.splitlines():
        m = re.search(r"(\d{9})\s+(.*)$", line)
        if not m:
            continue
        cj = re.match(r"([一-鿿])\s?([一-鿿])([一-鿿]?)", m.group(2).strip())
        if not cj or m.group(1) in seen:
            continue
        seen.add(m.group(1)); out.append([m.group(1), "".join(cj.groups()), ""])
    write("nthu-oop.csv", out)


def nthu_csv(path):
    """校務資訊系統匯出的選課名單：Big5，表頭＝學號、中文姓名、英文姓名、科系班別碼、科系班別中文、註記、電子郵件。
    沒有中文姓名的（外籍生）用英文姓名；信箱不收。"""
    raw = open(path, "rb").read()
    try:
        txt = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        txt = raw.decode("cp950")
    out, seen = [], set()
    rd = csv.DictReader(txt.splitlines())
    for r in rd:
        sid = (r.get("學號") or "").strip()
        # Big5 沒有的罕用字，校務系統會寫成 &#NNNNN;；複姓或兩段式姓名中間可能夾空白，漢字之間的空白拿掉
        name = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", html.unescape((r.get("中文姓名") or "").strip()))
        name = name or " ".join((r.get("英文姓名") or "").split())
        if "\ufffd" in name:
            stop(path, rd.line_num, f"姓名的 &#NNNNN; 組出 U+FFFD（學號 {mask_id(sid)}），多半是代理對寫法，名冊不能帶亂碼")
        if not re.fullmatch(r"\d{9}", sid):
            skip(path, rd.line_num, f"學號格式不是 9 位數字（{mask_id(sid)}）"); continue
        if not name:
            skip(path, rd.line_num, f"中文姓名與英文姓名都空白（學號 {mask_id(sid)}）"); continue
        if sid in seen:
            continue
        seen.add(sid); out.append([sid, name, (r.get("科系班別中文") or "").strip()])
    write("nthu-oop.csv", out)


NTUB_FILES = {"設計思考": "ntub-designthinking.csv", "設計脈絡與實踐": "ntub-contextdesign.csv"}


def ntub(xls):
    """PoolExport.xls 其實是 HTML，一個檔可含多門課、每門課分好幾頁；依「科目名稱：」標頭切段。列號＝檔內第幾個 <tr>。"""
    s = open(xls, encoding="utf-8", errors="replace").read()
    by_course, cur = {}, None
    for n, r in enumerate(re.findall(r"<tr.*?</tr>", s, flags=re.S | re.I), 1):
        c = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S | re.I)]
        odd = next((x for x in c if x.startswith("科目名稱") and not x.startswith("科目名稱：")), None)
        if odd:
            stop(xls, n, f"不認得的科目標頭寫法（預期「科目名稱：」全形冒號），照舊解析會把後面的人併進上一門課")
        head = next((x for x in c if x.startswith("科目名稱：")), None)
        if head:
            cur = head.split("：", 1)[1].strip(); by_course.setdefault(cur, [])
            continue
        is_data = len(c) > 5 and (c[0].isdigit() or re.fullmatch(r"\d{4}[A-Z]\d{3}", c[1]))
        if not is_data:
            continue
        if cur is None:
            stop(xls, n, "資料列出現在任何「科目名稱：」標頭之前，無法判斷是哪一門課")
        if not re.fullmatch(r"\d{4}[A-Z]\d{3}", c[1]):
            skip(xls, n, f"學號格式不是 4 位數字＋1 個大寫字母＋3 位數字（{mask_id(c[1])}）（{cur}）"); continue
        if not c[2]:
            skip(xls, n, f"姓名空白（學號 {mask_id(c[1])}）（{cur}）"); continue
        by_course[cur].append([c[1], c[2], c[5]])
    for course, rows in by_course.items():
        name = NTUB_FILES.get(course)
        if not name:
            print("未知課程，略過：", course, len(rows)); continue
        write(name, rows)


def cd(csv_path):
    rows = [[r["student_id"], r["name"], r.get("class", "")] for r in csv.DictReader(open(csv_path))]
    write("ntub-contextdesign.csv", rows)


a = argparse.ArgumentParser(); a.add_argument("--nthu-pdf"); a.add_argument("--nthu-csv"); a.add_argument("--ntub-xls"); a.add_argument("--cd-csv")
a.add_argument("--allow-skip", action="store_true", help="有格式外的列被跳過也不以非零結束（跳過的列照樣印出來）")
n = a.parse_args()
if n.nthu_pdf: nthu(n.nthu_pdf)
if n.nthu_csv: nthu_csv(n.nthu_csv)
if n.ntub_xls: ntub(n.ntub_xls)
if n.cd_csv: cd(n.cd_csv)
if SKIPPED:
    print(f"跳過 {len(SKIPPED)} 列（格式外，未寫進名冊）：")
    for fname, line, why in SKIPPED:
        print(f"  {fname} 第 {line} 列：{why}")
    if not n.allow_skip:
        sys.exit("有列被跳過，名冊檔一個都沒寫（舊檔原樣保留）：先確認那些人是不是真的該少，確定了再加 --allow-skip 重跑")
flush()
