# Security

This system decides who counts as present and stores student IDs, names and sign-in records, so two kinds of problems matter most:

- anything that lets a student sign in for someone else without the teacher seeing it (a way around the one-phone binding, the rotating QR, the PIN, or the spot checks);
- anything that exposes one student's data to another student or to someone without the teacher password.

## Reporting

Please report vulnerabilities privately through **Report a vulnerability** on the repository's [Security tab](https://github.com/chenweichiang/classroom-attendance/security), not in a public issue. Include the version or commit, the steps to reproduce, and what an attacker gains. You should get a reply within a week. Once a fix is released, the report can be made public, with credit if you want it.

## Supported versions

Only the latest release receives fixes.

## Scope notes

Some limits are documented design decisions rather than vulnerabilities: a student who hands their unlocked phone to a classmate, and the IP comparison being a flag rather than a block. The README's "How it works" table lists what each layer does and does not stop. Deployment mistakes described in the README (for example not stripping `CF-Connecting-IP` when you are not behind Cloudflare) are configuration issues, but reports that the documentation is unclear about them are welcome.

---

# 安全性

這套系統決定誰算出席，也存著學生的學號、姓名與簽到紀錄，所以最在意兩類問題：

- 能讓學生替別人簽到、而老師看不出來的方法（繞過一機一號綁定、輪換 QR、PIN 或抽點）；
- 能讓一位學生讀到別人的資料，或沒有老師密碼也能讀到資料的方法。

## 回報方式

請到 repo 的 [Security 分頁](https://github.com/chenweichiang/classroom-attendance/security)按 **Report a vulnerability** 私下回報，不要開公開 issue。請附上版本或 commit、重現步驟，以及攻擊者能得到什麼。一週內會回覆。修正發布後可以公開，要具名致謝也可以。

## 支援的版本

只有最新版會收到修正。

## 不算漏洞的範圍

有些限制是寫明的設計取捨：學生把解鎖的手機交給同學代簽、IP 比對只標記不擋下。各層擋得住與擋不住什麼，列在 README「怎麼擋」那張表。README 寫到的部署錯誤（例如不在 Cloudflare 後面卻沒拿掉 `CF-Connecting-IP`）屬於設定問題，但如果是文件寫得不清楚，也歡迎回報。
