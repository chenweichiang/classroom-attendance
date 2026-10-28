# Changelog

版號採語意化版本。Versions follow semantic versioning.

## v1.0.4 — 2026-10-29

- 老師主頁最下面顯示版號，回報問題時對得上是哪一版；新增測試，檢查版號和這份 CHANGELOG 最上面那一版一致。
  The teacher's home page shows the version at the bottom, so a bug report can say which release it is about; a new test checks that it matches the top entry of this CHANGELOG.
- Docker 映像改成可重現的建置：基底映像釘 digest，套件改裝新的 `requirements.lock`（完整相依、附雜湊，版本與測試用的一致）。快速開始也改用這份鎖檔安裝。
  Reproducible Docker build: the base image is pinned by digest and packages come from the new `requirements.lock` (every dependency, with hashes, at the versions the tests use). The quick start installs from the same lock.
- 新增行為準則（Contributor Covenant 3.0）、issue 表單（回報問題、建議）、pull request 範本與 Dependabot（GitHub Actions）；CI 用到的 Actions 改釘在 commit SHA。
  Added a code of conduct (Contributor Covenant 3.0), issue forms (bug report, suggestion), a pull request template and Dependabot for GitHub Actions; the Actions used in CI are pinned to commit SHAs.

## v1.0.3 — 2026-10-28

只修測試，程式行為不變。Tests only; behaviour is unchanged.

- 狀態機測試補上一條規則：綁定場次抽點判不在、綁定被撤回後，這個學號本場不能再綁定（回 403）。模型先前不知道這條，CI 隨機抽到這個順序時會失敗，縮小範例又撞上逾時，最後顯示成 Hypothesis 的 `FlakyStrategyDefinition`。
  State-machine test: the model now knows that once a spot check in a binding session marks a student absent and revokes the binding, that student cannot bind again in the same session (403). Without this, CI failed whenever the random run reached that sequence; shrinking then hit the time limit and the failure surfaced as a Hypothesis `FlakyStrategyDefinition` error.

## v1.0.2 — 2026-10-28

只更新文件與測試，程式行為不變。Documentation and tests only; behaviour is unchanged.

- 新增 `SECURITY.md`；README 補上參與貢獻與資安問題回報方式。
  Added `SECURITY.md`; the READMEs now say how to contribute and how to report a vulnerability.
- README 更新 uv 覆寫既有虛擬環境時的說明，拿掉已不適用的舊版附註。
  README: updated the note on uv refusing to overwrite an existing virtual environment and dropped an outdated remark about earlier versions.
- 移除舊的 `tests/run_all.sh`，改用 `tests/gate.sh --quick` 或 `--full`。
  Removed the old `tests/run_all.sh`; use `tests/gate.sh --quick` or `--full`.
- 狀態機測試修正模型：手機本場已被別的學號用過時，首次簽到不會寫入 PIN，模型改以資料庫為準。先前 `--full` 跑較多例子時可能偶發失敗。
  State-machine test: the model now reads the database to learn whether a first sign-in stored a PIN, which it does not when the phone was already used for another student in that session. `--full` runs could previously fail intermittently.

## v1.0.1 — 2026-09-26

四項修正，另新增 CI、補齊安裝文件。Four fixes, plus CI and fuller setup docs.

- `/healthz` 除了查詢資料庫，也檢查資料庫檔與所在目錄是否可寫（WAL 要在目錄裡建 `-wal`／`-shm`），不可寫就回 503。先前 `chown` 沒加 `-R` 這類權限錯誤可能通過健康檢查，要到開始點名才出錯。
  `/healthz` now also checks that the database file and its directory are writable (WAL creates `-wal`/`-shm` there) and returns 503 if not. A permission mistake such as a non-recursive `chown` could previously pass the health check and only fail once a session was opened.
- `.gitignore` 與 `.dockerignore` 排除 `*.csv`、`*.xls`、`*.xlsx`：名冊含學生個資，放在 repo 根目錄時不會被提交，也不會打包進映像檔。
  `.gitignore` and `.dockerignore` exclude `*.csv`, `*.xls` and `*.xlsx`, so a roster with student data left in the repository root is neither committed nor copied into the image.
- 鎖死回歸測試改量每一次請求的耗時（< 5 秒），不再看總耗時，較慢的 CPU 不會再誤報；CI 不再排除這條測試。
  The deadlock regression test now bounds each request (< 5 s) instead of the total, so slower CPUs no longer fail it; CI runs it again.
- `tests/gate.sh` 一開始就檢查 `.venv`、`curl` 與 load 層用的 `sqlite3`，缺了直接說缺什麼，不再誤報「伺服器啟動失敗」。
  `tests/gate.sh` checks for `.venv`, `curl` and, for the load layer, `sqlite3` before running anything, and names what is missing instead of reporting a failed server start.
- 新增 GitHub Actions：每個 PR 與 push 到 `main` 跑 `tests/gate.sh --quick`（Ubuntu 24.04、Python 3.12）。
  Added a GitHub Actions workflow running `tests/gate.sh --quick` on pull requests and pushes to `main` (Ubuntu 24.04, Python 3.12).
- README：補上全新 Ubuntu 需要的 `python3-venv`、uv、curl、sqlite3 與 `playwright install --with-deps`；範例名冊改放 `data/rosters/`，不再留在 repo 根目錄被提交或打包進映像檔；部署改用 `chown -R`，避免先跑過快速開始時資料庫唯讀。
  README: list what a fresh Ubuntu needs (`python3-venv`, uv, curl, sqlite3, `playwright install --with-deps`); keep the sample roster under `data/rosters/` so it is neither committed nor copied into the image; use `chown -R` so a database left by the quick start stays writable.

## v1.0.0 — 2026-09-21

第一個公開版本，內容等同作者 2026-09-21 部署在自己課堂上的版本。
First public release. Same code the author deployed in their own classes on 2026-09-21.

- 輪換 QR（10 秒換、過期後容許 30 秒）與同步更換的六位數備援碼。
  Rotating QR (10 s interval, 30 s grace) with a six-digit fallback code.
- 一個學號綁一支手機：伺服器下發 `HttpOnly` cookie，https 下帶 `__Host-` 前綴；PIN 只在綁定與換手機時使用。
  One student ID per phone via a server-issued `HttpOnly` cookie (`__Host-` prefixed over https); PIN only for binding and re-binding.
- 綁定場次：第一次上課只做綁定，不記缺席、不進報表。
  Binding sessions for the first meeting: no absences recorded, excluded from reports.
- 隨機抽點與代簽連坐；同裝置多學號一律轉待確認。
  Random spot checks; students sharing a phone are held as pending and go absent together.
- 出席與遲到以「掃到」的時間為準；出席窗可延長；逾時自動結案。
  Present or late is decided by scan time; the window can be extended; sessions close themselves.
- 點名頁即時簽到漏斗與「卡住的人」名單。
  Live sign-in funnel and a list of stuck students on the session page.
- 整場作廢、自動缺席整批更正、手動簽到、請假、解除綁定、重設 PIN。
  Void a session, bulk-correct automatic absences, manual sign-in, excused, unbind, PIN reset.
- 學期報表、CSV 匯出、學生自查頁 `/me`、Discord 缺席名單（選用）。
  Semester report, CSV export, student self-service page `/me`, optional Discord absence list.
- 測試閘門 `tests/gate.sh`（quick 九層、full 十三層）與教室彩排 `tests/rehearsal.py`。
  Test gate `tests/gate.sh` (nine layers quick, thirteen full) and the classroom rehearsal `tests/rehearsal.py`.
