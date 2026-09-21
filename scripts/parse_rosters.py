"""範例：把教務系統匯出的名冊轉成 data/rosters/*.csv（student_id、name、class 三欄），
再交給 manage.py import-roster。處理的是清大與北商的匯出格式，別的學校照自己的格式改。

  --nthu-csv  清大校務資訊系統匯出的選課名單文字檔（Big5、逗號分隔，姓名完整且帶班別）→ nthu-oop.csv
  --nthu-pdf  只有清大點名單 PDF 時的退路（用 pdftotext 抽字，姓名最多抓三個字）→ nthu-oop.csv
  --ntub-xls  北商 PoolExport.xls（內容其實是 HTML，一個檔可含多門課）；課名對應的輸出檔名寫在 NTUB_FILES
  --cd-csv    已經是 student_id、name、class 欄位的 CSV，轉存成 ntub-contextdesign.csv
              （跟 --ntub-xls 的其中一門寫同一個檔，兩個都給時以 --cd-csv 為準）

用法：python3 scripts/parse_rosters.py --nthu-csv STU_LIST.txt --ntub-xls PoolExport.xls
"""
import argparse, csv, html, re, subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "rosters"


def write(name, rows):
    OUT.mkdir(parents=True, exist_ok=True)
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
    for r in csv.DictReader(txt.splitlines()):
        sid = (r.get("學號") or "").strip()
        # Big5 沒有的罕用字，校務系統會寫成 &#NNNNN;；複姓或兩段式姓名中間可能夾空白，漢字之間的空白拿掉
        name = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", html.unescape((r.get("中文姓名") or "").strip()))
        name = name or " ".join((r.get("英文姓名") or "").split())
        if not re.fullmatch(r"\d{9}", sid) or not name or sid in seen:
            continue
        seen.add(sid); out.append([sid, name, (r.get("科系班別中文") or "").strip()])
    write("nthu-oop.csv", out)


NTUB_FILES = {"設計思考": "ntub-designthinking.csv", "設計脈絡與實踐": "ntub-contextdesign.csv"}


def ntub(xls):
    """PoolExport.xls 其實是 HTML，一個檔可含多門課、每門課分好幾頁；依「科目名稱：」標頭切段。"""
    s = open(xls, encoding="utf-8", errors="replace").read()
    by_course, cur = {}, None
    for r in re.findall(r"<tr.*?</tr>", s, flags=re.S | re.I):
        c = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S | re.I)]
        head = next((x for x in c if x.startswith("科目名稱：")), None)
        if head:
            cur = head.split("：", 1)[1].strip(); by_course.setdefault(cur, [])
            continue
        if cur and len(c) > 5 and re.fullmatch(r"\d{4}[A-Z]\d{3}", c[1]):
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
n = a.parse_args()
if n.nthu_pdf: nthu(n.nthu_pdf)
if n.nthu_csv: nthu_csv(n.nthu_csv)
if n.ntub_xls: ntub(n.ntub_xls)
if n.cd_csv: cd(n.cd_csv)
