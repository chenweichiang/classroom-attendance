# Contributing

Bug reports, questions and pull requests are welcome, in English or Traditional Chinese.

## Reporting a problem

Open an issue with the version or commit, what you did (teacher side or student side, which phone and browser), what you expected, what happened, and the server log lines from that moment if you run your own instance. Replace real student IDs and names with invented ones such as `9994A001` before posting.

A way for a student to sign in for someone else, or to read another student's data, is a security problem: report it privately as described in [`SECURITY.md`](SECURITY.md), not in an issue.

## Pull requests

- Keep each pull request to one change.
- Run `tests/gate.sh --quick` before opening it. Setup is in the README's "Tests" section, and CI runs the same gate on every pull request.
- If you change who may sign in or bind a device, update the model in `tests/test_state_machine.py` as well. It checks the server against its own copy of the rules, so a rule the model does not know about shows up as a failure.
- Test data uses invented IDs (`9994A…`, `999…`) and invented names. Never commit a real roster.

The code is maintained in the author's working copy and published here with each release. An accepted pull request is applied there and arrives in the next release instead of being merged directly.

---

# 參與貢獻

歡迎用英文或繁體中文回報問題、提問或送 pull request。

## 回報問題

開 issue 時請附上版本或 commit、做了什麼（老師端或學生端、哪支手機與瀏覽器）、預期結果、實際結果；自己架站的話，附上當下的伺服器日誌。貼出來之前，請把真實學號與姓名換成虛構的，例如 `9994A001`。

能讓學生替別人簽到、或讀到別人資料的方法屬於安全性問題，請照 [`SECURITY.md`](SECURITY.md) 私下回報，不要開 issue。

## Pull request

- 一個 pull request 只做一件事。
- 送出前請跑 `tests/gate.sh --quick`。安裝方式見 README「測試」一節，CI 在每個 pull request 都會跑同一套閘門。
- 改到誰能簽到、誰能綁定裝置的規則時，`tests/test_state_machine.py` 裡的模型也要一起改。它拿自己的一份規則和伺服器對照，模型不知道的規則會被當成失敗。
- 測試資料一律用虛構學號（`9994A…`、`999…`）與虛構姓名，真實名冊不進版控。

程式在作者的工作副本維護，每次發版時同步到這裡。接受的 PR 會在工作副本套用、隨下一版出現，不會直接合併。
