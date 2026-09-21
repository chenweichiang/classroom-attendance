# Changelog

版號採語意化版本。Versions follow semantic versioning.

## v1.0.0 — 2026-09-21

第一個公開版本，內容等同作者 2026-09-21 部署在自己課堂上的版本。
First public release. Same code the author deployed for his own classes on 2026-09-21.

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
