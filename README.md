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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Portal login appears to have failed` | Set repo variables/secrets `USER_SELECTOR` / `PASS_SELECTOR` to the right CSS selectors for the portal's login form. |
| Join button never found | Make sure `CLASS_KEYWORD` matches text on the class card exactly (e.g. `React JS`). |
| No sound in recording | Zoom may need the audio-join step; check the run log, and re-run a test. |
| Telegram upload fails | The bot must have started a chat with you (or be admin in the channel). Re-check `TG_CHAT_ID`. |
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
