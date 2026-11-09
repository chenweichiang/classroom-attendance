# Changelog

版號採語意化版本。Versions follow semantic versioning.

## v1.1.0 — 2026-11-09

功能更新，與作者自己課堂上使用的版本同步到 2026-11-09。資料庫結構沒有變：既有的 `attend.db` 直接沿用，啟動時的欄位檢查與 v1.0 相同，不用手動遷移；設定鍵沒有新增，`.env` 與每門課的設定都不用改。
Feature release, in sync with the version the author uses in their own classes as of 2026-11-09. The database schema is unchanged: an existing `attend.db` is used as is, the start-up column checks are the same as in v1.0 and no manual migration is needed. No setting keys were added, so neither `.env` nor per-course settings need editing.

學生端 Students

- 換手機或換瀏覽器時（iPhone 從控制中心的掃碼器、Safari 私密分頁或 LINE 裡開，每次都算新的瀏覽器），輸入原本的 PIN 就自動改綁到新的那個並照常記出席，名單標「換瀏覽器改綁」；原本會被「已綁定另一支手機」擋下而要找老師。同一個瀏覽器重新整理或重掃，直接回到只輸 PIN 的畫面，不再給設新 PIN 的表單。
  Moving to another phone or browser (on iPhone the Control Centre scanner, a Safari private tab and the LINE in-app browser each count as a new browser): entering the original PIN rebinds the ID to the new one and records attendance as usual, flagged "rebound from another browser" on the roster; previously this was rejected as "bound to another phone" and needed the teacher. Reloading or rescanning in the same browser returns straight to the PIN-only form instead of offering to set a new PIN.
- PIN 錯誤次數的計算對象講清楚：每支手機每場各自計，同一個錯 PIN 一分鐘內重送只算一次，打對就清零；學號打錯成同學的，那次錯誤不算到同學頭上（猜測上限照計）；老師按「重設」會一併解鎖。鎖定後按鈕停用，訊息寫明忘了 PIN 可請老師重設。
  Wrong-PIN counting is now explicit: per phone per session; the same wrong PIN resent within a minute counts once; a correct PIN clears the count; a wrong PIN typed under a mistyped ID that happens to be a classmate's is no longer attributed to that classmate (the guessing limit still applies); a teacher reset also unlocks. Once locked, the button is disabled and the message says a teacher can reset the PIN.
- 出席或遲到看這支手機本場最早掃到的時間，重掃或網路慢不會把出席變成遲到；借別人手機簽到的人不繼承出借者的時間。簽到已寫入、只是回應沒回來的人重送時直接拿回收據，不再看到「填表時間已到」或「點名已結束」。掃到已經結束那一場的舊 QR，直接顯示點名已結束；老師已開新的一場時，說明要重掃投影幕上的 QR。
  Present or late is decided by the earliest scan from that phone in the session, so a rescan or a slow network no longer turns present into late; a student on a borrowed phone does not inherit its owner's time. A student whose sign-in was recorded but whose response was lost gets the receipt on resend instead of "time is up" or "session closed". Scanning an old QR from a session that has ended shows the session-closed screen; if the teacher has opened a newer session, the page says to rescan the projector.
- 同一場裡，指紋（IP、瀏覽器、語言）與另一位已簽到成功者完全相同的首次綁定或改綁，在一般點名轉待確認並標「疑似同手機」，綁定場次只標記；用來擋私密分頁替人綁定。老師先記缺席或請假、學生才掃到的，告知已被記，名單標「老師改後才掃碼」。
  A first binding or rebind whose fingerprint (IP, browser, language) matches another successful sign-in in the same session is held as pending and flagged "same phone suspected" in a regular session, flag only in a binding session; this catches binding for someone else from a private tab. A student who scans after the teacher already marked them absent or excused is told so, and the row is flagged "scanned after teacher's change".
- 學生頁文案與版面：倒數同時看表單時限與場次結束時間；錯誤訊息放在輸入欄上方，不被鍵盤遮住；輸入欄 16px，iOS 不會放大整頁；說明頁的訊息對照表補齊；綁定場次的收據寫明不算出缺勤；重開上週的舊分頁不再顯示「已簽到」；`/me` 讀取有逾時。
  Student page copy and layout: the countdown follows both the form deadline and the session's end; error messages sit above the inputs where the keyboard cannot hide them; inputs are 16px so iOS does not zoom the page; the help page's message table is complete; the receipt from a binding session says it does not count toward attendance; an old tab from last week no longer claims "already signed in"; `/me` times out instead of spinning.

老師端 Teacher

- 取消結束：誤按「結束點名」後 10 分鐘內可按「取消結束」，自動記的缺席拿掉，已簽到的與你改過的都保留，出席窗與遲到窗至少再開 2 分鐘；這門課之後已開新的一場就不能取消。回主頁按「開始點名」時，上一場 10 分鐘內才結束會先提醒。
  Undo close: within 10 minutes of pressing "Close" by mistake, "Undo close" removes the automatic absences, keeps every sign-in and every manual change, and reopens the attendance and late windows for at least 2 more minutes; once a later session of the course has been opened it can no longer be undone. Pressing "Start" on the home page warns if the previous session closed less than 10 minutes ago.
- 出席窗剩不到一分鐘時紅字提醒「還有人在填就按延長」；按「結束點名」前的確認框說還有幾支手機在填表；延長、抽點與發 Discord 的請求在回應遺失時不會重複生效。
  A red hint "extend if people are still filling in" appears in the last minute of the attendance window; the confirmation before "Close" says how many phones are still filling in the form; extend, spot-check and Discord requests whose response was lost no longer take effect twice.
- 卡住名單旁直接「重設 PIN」，不必到名冊頁；點卡住的人會把學號帶進手動簽到；手動簽到顯示每個候選人目前的狀態、記完有回饋、已經是出席或遲到的人要改成別的狀態時先確認，全形學號也找得到人。名單上「掃碼被擋：原因」一直留到他簽成功或你處理為止；開場後才加選的人顯示「開場後加選」，不算未簽到。
  "Reset PIN" sits next to the stuck list, so the roster page is no longer needed; tapping a stuck student fills their ID into manual sign-in; manual sign-in shows each candidate's current status, confirms what it recorded, asks before changing a student who is already present or late to another status, and finds full-width IDs. The "scan rejected: reason" tag stays on the row until the student succeeds or you act; students added after the session opened show "added after start" and are not counted as missing.
- 剛被你手動記過或重設過的同學，10 分鐘內再打錯 PIN 顯示灰字「剛處理過的同學又試了一次」，不再是紅字「請當場確認本人」。「IP 與教室不同」不再顯示：老師與學生多半各用行動網路，比對沒有判讀價值；資料照存。
  A student you just recorded manually or reset who gets a PIN wrong within 10 minutes shows in grey as "handled a moment ago, tried again" rather than the red "confirm in person". The "IP differs from classroom" flag is no longer shown: teacher and students are mostly on separate mobile networks, so it carried no signal; the data is still stored.
- Discord：手動按「結束點名」也套用「出席不到一半就暫緩」；進行中可按「這場先不要自動發」；名單發出後又改了狀態會提示再按「發到 Discord」；重複按不會重發同一份；失敗原因以中文說明，逾時會提醒名單可能已經發出；主頁場次列標出「Discord 沒發出」與「暫緩中」。
  Discord: closing by hand now also holds the list when fewer than half are present; "do not auto-post this session" can be pressed while the session is open; changing a status after the list went out prompts you to press "Post to Discord" again; pressing it twice does not post the same list twice; failures are explained in Chinese and a timeout warns that the list may already be out; the home page tags sessions as "not posted" or "held".
- 綁定場次抽點判「不在」：那個學號當天都不能再綁（原本只擋本場），按「不在」前先跳確認框；已作廢的綁定場次裡的抽點結果不算。抽點「在」把遲到區簽到的待確認還原成遲到；名單標「遲到區簽到」提醒你手動放行時該選遲到。
  Spot check in a binding session: a student marked "not here" cannot bind again that day (previously only in that session), and "not here" asks for confirmation first; spot checks in a voided binding session no longer count. "Here" restores a pending student who scanned in the late window to late, and a "scanned in late window" tag reminds you to choose late when releasing such a student by hand.
- 名冊頁：搜尋學號或姓名；「解除綁定」與「重設」的確認框列出學號姓名，完成後的提示依情況說明他接下來怎麼綁；「重設」一併清掉本場的 PIN 鎖定。首次綁定開關只管從沒設過 PIN 的人：已設過 PIN 的人輸對 PIN 一律能綁，當天被重設的人也不受開關限制。
  Roster page: search by ID or name; the "Unbind" and "Reset" confirmations show the ID and name, and the message afterwards says how that student binds next; "Reset" also clears the session's PIN lock. The first-binding switch now only governs students who have never set a PIN: anyone with a PIN can bind by entering it, and a student reset that day is also exempt.
- 報表每一場的日期都連到該場點名頁，主頁多了「全部場次」連結，舊場次不再進不去；點名頁與名冊頁在手機直立時不再溢出畫面。
  Every session date in the report links to that session's page and the home page gained an "all sessions" link, so older sessions are reachable again; the session and roster pages no longer overflow on a portrait phone.
- 投影頁連不上伺服器時顯示已連不上幾秒並自動重試，不再叫老師重新整理；點名頁顯示投影頁的連線狀態；投影頁在手機直立時 QR 與六位數完整可見。
  When the projector page cannot reach the server it shows for how many seconds and keeps retrying instead of asking for a reload; the session page shows the projector's connection status; the projector page fits a portrait phone with the QR and the six digits fully visible.
- 登出改成確認頁上的按鈕（POST），開啟連結本身不再有副作用；先前任何人只要讓老師的瀏覽器載入那個網址，就能把所有裝置登出。
  Logging out is now a button on a confirmation page (POST); opening the link itself no longer has a side effect. Previously anyone who got the teacher's browser to load that URL could log out every device.

部署與維運 Deployment and operations

- `manage.py import-roster` 先把整份 CSV 檢查完才寫資料庫，任何一項不過就什麼都不改。新增 `--dry-run`；逐列印出改名與改班級；已綁定或已設 PIN 的人改名、`--prune` 要刪的人超過名冊一成、要刪與要新增的人同名、課程代碼疑似打錯，都要加 `--force`；疑似被截斷的姓名與不合格的學號一律擋下。學號先正規化（全形轉半形、去空白與零寬字元、轉大寫）。
  `manage.py import-roster` validates the whole CSV before writing anything; if any check fails, nothing changes. New `--dry-run`; renamed students and class changes are printed line by line; renaming a bound student or one with a PIN, pruning more than a tenth of the roster, a pruned and an added student sharing a name, and a course code that looks mistyped all require `--force`; names that look truncated and malformed IDs are always refused. Student IDs are normalised first (full-width to ASCII, whitespace and zero-width characters stripped, upper-cased).
- `manage.py set` 拒絕超出範圍的值並印出實際會生效的數字，不再寫入一個伺服器其實會夾回去的值。
  `manage.py set` refuses out-of-range values and prints the value that would actually take effect, instead of storing one the server silently clamps.
- 範例名冊解析腳本 `scripts/parse_rosters.py`：格式外的列（學號格式不對、姓名空白）逐列印出檔名、列號與原因，不印姓名；有任何一列被跳過就一個名冊檔都不寫並以非零結束，確定要放行才加 `--allow-skip`。
  The example roster parser `scripts/parse_rosters.py` prints every row it cannot use (malformed ID, blank name) with file, line and reason, never the name; if any row is skipped it writes no roster file and exits non-zero unless `--allow-skip` is given.
- 新增 `tools/session_report.py`：唯讀、匿名化的場次診斷報告，從事件表排出一場點名裡誰在哪一步被擋、老師做了哪些操作；`tools/log_funnel.py` 修正錯誤原因對照與裝置判別。學生看得到的失敗與老師的操作（延長、手動改狀態、解除綁定、重設、首次綁定開關、取消結束）都留事件紀錄。
  New `tools/session_report.py`: a read-only, anonymised session report that lays out from the event log where each student got stuck and what the teacher did; `tools/log_funnel.py` fixes its error-reason table and device detection. Every failure a student can see and every teacher action (extend, manual status, unbind, reset, first-binding switch, undo close) now leaves an event record.
- 帶版本號的字型網址快取一年（`immutable`），其餘靜態檔維持一週；字型換了網址就換，不會拿到舊檔。
  The versioned font URL is cached for a year (`immutable`); other static files keep the one-week cache. A new font gets a new URL, so a stale copy is never served.
- 限流：IPv6 以 /64 計，換位址繞不過每 IP 上限；`/c` 的每出口上限只算錯碼，已綁定的手機不受影響；掃 QR 開出簽到頁每個出口每分鐘 120 次。
  Rate limits: IPv6 is counted per /64, so changing address does not evade the per-IP limit; the per-exit limit on `/c` counts only wrong codes and no longer affects bound phones; QR landings are limited to 120 per exit per minute.

測試 Tests

- unit 層併入學生端與老師端摩擦回歸、`manage.py`、兩支診斷工具與測試外殼的測試；flaky 層改重跑 `test_api.py` 與 `test_teacher_friction.py`；state 層 `--full` 跑 300 例；教室彩排跑兩組亂數種子，並模擬換瀏覽器與忘 PIN 的學生、老師在卡住名單處理的動作，另有 `--same-network` 看全班同網路時的待確認人數。
  The unit layer now includes the student and teacher friction regressions and the tests for `manage.py`, the two diagnostic tools and the test harness; the flaky layer reruns `test_api.py` and `test_teacher_friction.py`; the state layer runs 300 examples under `--full`; the classroom rehearsal runs two seeds and simulates students who switch browsers or forget their PIN and a teacher working the stuck list, with `--same-network` to count pending students when the whole class shares one network.
- 測試素材裡的中文全名只准出現 `tests/FAKE_NAMES.txt` 裡的虛構姓名，由一條測試檢查；突變測試改為就地還原原始碼，不再誤報全部殺死。
  Chinese full names in test material must come from `tests/FAKE_NAMES.txt`, enforced by a test; mutation testing restores the source in place, so it no longer falsely reports every mutant killed.

## v1.0.5 — 2026-10-29

安全性更新，請盡快更新已部署的站台。Security update; redeploy existing instances.

- `python-multipart` 從 0.0.20 升到 0.0.32，修補七個已公開的弱點，其中三個是高風險（以大量或畸形的表單資料造成服務阻斷，以及非預設設定下的任意檔案寫入）。點名系統的登入、簽到與設定都經過表單解析，所以會受影響。
  `python-multipart` goes from 0.0.20 to 0.0.32, fixing seven published vulnerabilities, three of them high severity (denial of service through large or malformed form data, and an arbitrary file write under a non-default configuration). Sign-in, check-in and settings all go through form parsing, so this system was exposed.

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
