# Classroom Attendance

**English** | [繁體中文](README.zh-TW.md)

Self-hosted attendance for in-person classes. The projector shows a QR code that changes every ten seconds, each student ID is bound to one phone, and the teacher spot-checks a few random names. One FastAPI file, one SQLite database, one Docker container.

It is built against two things: a classmate signing in for someone who is absent, and signing in from outside the room.

<p>
  <img src="docs/img/projector.png" width="66%" alt="Projector page: a large QR code, the six-digit fallback code, seconds until the next rotation, and the signed-in count">
  <img src="docs/img/student-receipt.png" width="25%" alt="Student receipt on a phone: a red seal reading 'present', the student's name, ID and time">
</p>

*Left: the projector page. Right: what a student sees after signing in. All names and IDs in the screenshots are invented.*

> The interface is in Traditional Chinese (Taiwan) only. Code comments and the teacher's handbook are in Chinese too. This README is the English entry point.

## How it works

Five layers, from cheapest to most human:

| Layer | What it does | What gets past it |
|---|---|---|
| Rotating QR | HMAC-signed time slice, new code every 10 s, still accepted for 30 s after it rotates out. A six-digit code under the QR rotates in step, for phones whose camera cannot read the screen (`/c`). | A photo relayed to someone outside, for at most 40 s |
| One ID, one phone | On first sign-in the phone gets a server-issued `HttpOnly` cookie (400 days, `__Host-` prefixed over https) and the student ID is bound to it. A bound ID cannot sign in from another phone. If a second ID signs in from a phone that is already bound, it is not bound to that phone, and both students are held as *pending* until the teacher has looked at them. The teacher can unbind. | Handing over your unlocked phone |
| PIN | 4 to 6 digits, chosen at binding, stored as PBKDF2-SHA256. Needed only when binding or moving to a new phone, never on a normal day. | — |
| Random spot check | The session page draws N names and shows them in large type. The teacher calls each name and looks up. "Not here" turns that student absent, and anyone who signed in from the same phone goes absent with them. | — |
| IP comparison | Student's public IP against the projector page's. Flag only, never blocks. Hidden automatically when most of the class is on mobile data, since the flag then means nothing. | Anyone on mobile data |

The honest summary: layers 1 to 3 make cheating inconvenient, and layer 4 is what actually catches it. If you never spot-check, a student holding a friend's unlocked phone can sign in for them.

## What a class looks like

**First meeting of a course**: run a *binding session*. Students scan, enter student ID, full name and a PIN twice. It stays open ten minutes, records no absences and never shows up in reports. Names that match the roster in length and first character are accepted and flagged for the teacher to glance at, so variant characters do not lock anyone out.

**Every meeting after that** (about four minutes of the teacher's attention):

1. Open `/t` on the laptop, press *Start*, drag the projector page to the projector.
2. Students scan. Their name is already on the page; one tap and they are done. No PIN.
3. The attendance window (default 3 min) closes on its own; late arrivals can still sign in as *late* until the late limit (default 15 min). What counts is when the phone *scanned*, not when the form was submitted.
4. Press *Spot check*, call the names, press *here* or *not here*.
5. Press *Close*. Everyone who did not sign in is marked absent.

The session page shows a live funnel (phones that scanned, scans that arrived expired, submissions, successes) and a list of students who have been rejected twice or more, with the reason. When the count stops moving, this is where you see which step is losing people.

<p><img src="docs/img/teacher-session.png" width="80%" alt="Teacher's session page: counts by status, spot-check names in large type with here / not here buttons, and the live roster with flags"></p>

*The teacher's session page during a spot check. The red flags are genuine detections: this demo drove nine simulated phones from one machine, which is what the same-device fingerprint check exists to notice.*

Also there: manual sign-in for a dead phone, excused absences, voiding a whole session when the projector or network failed, bulk-correcting automatic absences, per-student unbind and PIN reset, a semester report, CSV export, a self-service page where students see their own record (`/me`), and optional posting of the absence list to a Discord channel.

## Quick start

Local, no Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

export ATTEND_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
export ATTEND_TEACHER_PASSWORD=change-me

printf 'student_id,name,class\nS001,王小明,Demo\nS002,陳大文,Demo\n' > roster.csv
.venv/bin/python manage.py import-roster --code DEMO --name "Demo course" --csv roster.csv
.venv/bin/uvicorn app:app --port 8000
```

Open <http://127.0.0.1:8000/t> and log in. The QR encodes `ATTEND_BASE_URL` (default `http://127.0.0.1:8000`), so a real phone can only follow it once that variable points at an address the phone can reach.

Roster CSV columns are `student_id`, `name`, `class` (the Chinese headers `學號`, `姓名`, `班級` also work). Importing again is safe: existing students keep their PIN and phone binding; pass `--prune` only when someone has really dropped the course, because it deletes their records.

## Deployment

```bash
cp .env.example .env && chmod 600 .env      # fill in the three required values
mkdir -p data && sudo chown 10001:10001 data
docker compose build && docker compose up -d
```

The container listens on `127.0.0.1:3009`, runs as a non-root user with a read-only filesystem and all capabilities dropped. Put a TLS-terminating reverse proxy in front of it. With Caddy:

```caddyfile
attend.example.edu {
	request_header -CF-Connecting-IP   # remove this line if you ARE behind Cloudflare
	reverse_proxy 127.0.0.1:3009
}
```

Things that will bite you if you skip them:

- **Client IP.** The app takes the client address from `CF-Connecting-IP` first, then from the last hop of `X-Forwarded-For`. Behind Cloudflare that is correct, provided your origin only accepts Cloudflare's address ranges. Without Cloudflare, strip `CF-Connecting-IP` at your proxy as shown above, or any student can forge the address used for the IP flag and for rate limiting.
- **One worker.** Rate limits live in process memory. Running several workers silently disables them. The Dockerfile already starts a single worker; keep it that way.
- **Never rotate `ATTEND_SECRET` mid-semester.** Device hashes are derived from it, so every phone binding in every course breaks at once. To invalidate the teacher's login, use *Log out*, which revokes all teacher sessions.
- **CDN caching.** Every page and API response carries `Cache-Control: private, no-store` (static files are cached for a week, with a content hash in the font URL), and the CSV export lives at `/export/csv` with no file extension, because CDNs that cache by extension will otherwise keep a report full of student names at the edge for hours.
- **Build first, then restart.** `docker compose build` followed by `up -d` keeps the outage to a few seconds. Do not deploy while a session is open.

### Configuration

Environment variables:

| Variable | Required | Meaning |
|---|---|---|
| `ATTEND_SECRET` | yes | Signing key for QR tokens, teacher tokens and device hashes |
| `ATTEND_TEACHER_PASSWORD` | yes | The single teacher password |
| `ATTEND_BASE_URL` | yes in production | Public URL students reach. Drives the QR contents, cookie naming (`__Host-` only over https) and the addresses printed on help pages |
| `ATTEND_DB` | no | SQLite path. Defaults to `/data/attend.db` in the container, `./data/attend.db` otherwise |
| `DISCORD_TOKEN` | no | Bot token, only if you want absence lists posted to Discord |

Per-course settings, editable on the roster page or with `manage.py set`:

| Key | Default | Meaning |
|---|---|---|
| `open_minutes` | 3 | Window in which a scan counts as *present* |
| `late_minutes` | 15 | Until when a scan counts as *late*, measured from the start |
| `bind_minutes` | 10 | Length of a binding session (3 to 30) |
| `qr_interval` | 10 | Seconds between QR rotations (minimum 5) |
| `qr_grace` | 30 | Seconds a rotated-out QR is still accepted (maximum 120) |
| `grant_seconds` | 180 | Time allowed to fill in the form after scanning |
| `spot_n` | 3 | Names drawn per spot check |
| `pin_max_fail` | 3 | Wrong PINs per phone before that phone is locked for the session |
| `ip_check` | true | Compare student and projector public IPs (flag only) |
| `allow_bind` | true | Allow first-time binding. Turn off after week one so nobody can claim an unbound ID |
| `discord_channel` | empty | Channel ID for the absence list |

Command line:

```bash
python manage.py list
python manage.py import-roster --code DEMO --name "Demo course" --csv roster.csv [--prune]
python manage.py set --code DEMO --key qr_grace --value 30
python manage.py delete-course --code DEMO
```

In Docker, prefix with `docker compose exec -T attend`. A roster can be piped in without touching the server's disk: `cat roster.csv | ssh host 'cd /path && docker compose exec -T attend python manage.py import-roster --code DEMO --name "Demo" --csv /dev/stdin'`.

`scripts/parse_rosters.py` is an example of turning registrar exports into that CSV. It handles two formats used in Taiwan (NTHU's Big5 class list and NTUB's HTML-disguised-as-XLS export); expect to adapt it to your own school.

## Student data

The database holds student IDs, names, class labels, salted PIN hashes, device hashes, and for every sign-in the time, public IP and flags. It is a plain SQLite file. Keep backups encrypted or on storage you control, check what your institution requires before hosting it off campus, and delete the database when the semester ends (`manage.py delete-course`, or remove `data/attend.db` for a clean slate).

Nothing in this repository is real student data. Test fixtures use invented IDs (`9994A…`, `999…`) and invented names.

## Tests

```bash
uv venv .venv && uv pip install -p .venv/bin/python -r requirements.txt -r tests/requirements-dev.lock
.venv/bin/python -m playwright install chromium webkit
tests/gate.sh --quick     # nine layers, a few minutes
tests/gate.sh --full      # adds real-clock timing, load, classroom rehearsal, flakiness
```

The layers are static analysis, unit tests with 97% branch coverage, a Hypothesis state machine holding seven invariants, scenario scripts, Schemathesis fuzzing, and Playwright end-to-end runs with Chromium as the teacher and WebKit as an iPhone. Details are in [`tests/README.md`](tests/README.md) (Chinese).

Coverage is not the metric this project trusts. The first classroom trial ran with every one of those layers green, and six of the nine phones that connected managed to sign in. Server logs showed why: 5 of 17 scans arrived after the QR had already expired, all five on Android phones. Getting from the camera recognizing the code to the browser sending its request took 5 to 20 seconds, and at the time a rotated-out QR stayed valid for only 5. So the suite gained `tests/rehearsal.py`, which drives thirty simulated phones through a real browser with a delay model fitted to those logs, and the pass criterion became classroom outcomes: at least 95% of the class signed in, 90th-percentile time under 60 seconds.

| Rehearsal, 30 students, two seeds | Before | After |
|---|---|---|
| First meeting: students who completed binding | 87–90% | 97–100% |
| Scans arriving already expired | 24–36% | 0% |
| Regular meeting: 90th-percentile time to sign in | 37–42 s | 19–33 s |

These are simulation figures. The first real class on the reworked version was small: six phones scanned, none arrived expired, all six bound successfully.

## Layout

```
app.py                 the whole server: routes, schema, migrations, rate limits
manage.py              roster import and course settings CLI
templates/  static/    Jinja pages and the subset display font
tests/                 gate.sh, the test layers, rehearsal.py
tools/log_funnel.py    rebuild the sign-in funnel from reverse-proxy access logs
tools/subset_font.py   rebuild the font subset when page text or roster names change
scripts/parse_rosters.py   example registrar-export parsers
docs/OPERATIONS.zh-TW.md   the teacher's handbook (Chinese)
```

## License

Code is released under the [MIT License](LICENSE).

`static/fonts/ZhuqueFangsong-subset.woff2` is a subset of [Zhuque Fangsong](https://github.com/TrionesType/zhuque), © 2023 JadeFoci, distributed under the SIL Open Font License 1.1; the license text is in [`static/fonts/OFL.txt`](static/fonts/OFL.txt).

## Author

Chenwei Chiang, Department of Creative Technologies and Product Design, National Taipei University of Business. Written for his own courses and used in them every week.
