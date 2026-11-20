# naresh-zoom-recorder

Records your daily live class from the student portal on GitHub's free servers
(no device of your own needed) and sends the 1080p MP4 to your Telegram.

How it works: GitHub Actions starts a container at 7:22 PM IST, logs in to the
portal, waits for the **Join** button to become active, joins the Zoom web
client with mic and camera off, records the screen and audio with FFmpeg, then
uploads the video to Telegram (files over ~1.95 GB are split automatically).

```
.
├── Dockerfile
├── requirements.txt
├── recorder.py
├── popup_guard.js          # in-page popup / cookie-banner dismisser
├── .env.example
└── .github/workflows/
    ├── build-image.yml     # builds the image once into ghcr.io
    └── record-class.yml    # daily 7:22 PM IST + manual run
```

## One-time setup

### 1. Telegram
1. Open **@BotFather** -> `/newbot` -> copy the **bot token** (`TG_BOT_TOKEN`).
2. Go to **https://my.telegram.org** -> *API development tools* -> create an app
   -> copy **api_id** (`TG_API_ID`) and **api_hash** (`TG_API_HASH`).
3. Find your chat ID (`TG_CHAT_ID`):
   - Send any message to your new bot (press **Start**), then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`; or
   - Use a private channel: add the bot as an **admin** and use the channel id
     (looks like `-100xxxxxxxxxx`).

### 2. GitHub
1. Create a **public** repository and upload all files in this folder
   (keep the `.github/workflows` path exactly).
2. **Settings -> Secrets and variables -> Actions -> Secrets**, add:
   `PORTAL_URL`, `PORTAL_USER`, `PORTAL_PASS`, `TG_API_ID`, `TG_API_HASH`,
   `TG_BOT_TOKEN`, `TG_CHAT_ID`.
3. Optional, under **Variables**: `CLASS_KEYWORD` (default `React JS`) and
   `CLASS_START` (default `19:30`, IST, 24-hour).
4. **Settings -> Actions -> General -> Workflow permissions**: allow
   *Read and write permissions* (needed to publish the image).

### 3. Build the image
Pushing to `main` runs **Build recorder image** automatically. Wait for the
green tick in the **Actions** tab. If you want the image private-safe, leave
its package visibility as is; the daily workflow logs in with the built-in token.

### 4. Test before relying on it
**Actions -> Record class -> Run workflow.** Set `class_keyword` and
`class_start` to a class that is live right now (or starting within 45 minutes).
Watch the Telegram alerts and the run log. Fix any selector problems (see below).

## 24/7 Cloud Scheduler Web App (Optional, Recommended)
A dedicated Cloudflare Worker scheduler is deployed for this repo at:
👉 **[https://class-scheduler.class-scheduler.workers.dev/](https://class-scheduler.class-scheduler.workers.dev/)**

- **Exact IST Minute Scheduling:** Cloudflare triggers the GitHub Actions API at the exact IST minute, avoiding GitHub's cron jitter.
- **Bypasses 60-Day Inactivity Pause:** Runs 24/7 on Cloudflare's edge without needing manual re-activation.
- **Multi-Class Support:** Set schedules for both **Advance Java** (`18:00` - `19:30`) and **React JS** (`19:30` onwards).
- **One-Tap Mobile Control:** Open from your phone, sign in with GitHub, toggle schedules, or tap **▶️ Test Run Now**.

## Join flow
1. Log in to the portal -> 2. open **Live Classes** -> 3. wait until the Join button is
clickable and click it -> 4. on the Zoom page click **Join from browser** ->
5. pre-join screen: name, mic muted, video off, **Join** -> 6. record until the
meeting ends or `MAX_MEETING_SEC`. If Zoom shows a sign-in page instead, a screenshot is
sent to Telegram and nothing is recorded (optional `ZOOM_EMAIL` / `ZOOM_PASS` secrets).

## Popup protection (Chrome + Zoom)
- **Extra tabs/windows**: any tab that is not the portal or Zoom is closed within 1 s;
  once Zoom opens, it is the only tab allowed (and is brought to front).
- **Zoom pages**: `window.open()` is allowed only for Zoom's own https pages (so
  "Join from browser" works); `zoommtg://` launches and foreign popups are blocked.
  The "Open xdg-open?" app prompt is suppressed by a Chromium policy
  (`chromium-policy/zoom.json`). Zoom links are never rewritten.
- **Browser prompts**: notifications, mic/camera, password-save, translate,
  crash bubbles and similar Chrome bubbles are disabled; JS alert/confirm dialogs are auto-handled.
- **In-page popups**: cookie banners, "Got it", "Not now", recording notice,
  "Stay muted", toasts and similar are auto-dismissed by `popup_guard.js`.
  It never touches dialogs with input fields, passcode/name prompts, Join Audio,
  or anything about Leave/End.
- **Trackers/ads** (Google Analytics, DoubleClick, Hotjar, ...) are blocked. Set
  variable `BLOCK_TRACKERS=0` if the portal ever misbehaves; use `ALLOWED_HOSTS`
  to permit extra domains.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Portal login appears to have failed` | Set repo variables/secrets `USER_SELECTOR` / `PASS_SELECTOR` to the right CSS selectors for the portal's login form. |
| Join button never found | Make sure `CLASS_KEYWORD` matches text on the class card exactly (e.g. `React JS`). |
| No sound in recording | Zoom may need the audio-join step; check the run log, and re-run a test. |
| Telegram upload fails | The bot must have started a chat with you (or be admin in the channel). Re-check `TG_CHAT_ID`. |
| Portal tab closed unexpectedly | Add the portal's other domain to the `ALLOWED_HOSTS` variable. |
| Schedule did not run | GitHub pauses schedules after 60 days with no repo activity, and may start a few minutes late. Re-enable in the Actions tab. |

## Notes
- Public repo logs are public. The script masks secrets and meeting-link
  passwords, but never `print` extra details if you edit it.
- Check that your institute allows recording its classes.
- To skip Sundays, change the cron to `52 13 * * 1-6`.

## Local test (optional, needs Docker)
```bash
cp .env.example .env        # fill in the values
docker build -t naresh-zoom-recorder .
docker run --rm --init --shm-size=2g --env-file .env naresh-zoom-recorder
```
