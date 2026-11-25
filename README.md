# 🎥 Zoom Class Recorder with Remote Desktop Access (noVNC)

Autonomous zero-device 1080p live class recorder running on GitHub Actions cloud infrastructure. Features a real-time, touch-friendly **Remote Desktop (noVNC)** web interface accessed securely through a **Cloudflare Named Tunnel** and **Cloudflare Access**, allowing you to monitor and manually interact with Chrome on your phone or laptop whenever Zoom prompts for human verification, sign-in, or CAPTCHA.

Completed recordings are saved as high-quality 1080p MP4 files with synchronized PulseAudio sound, uploaded directly as **GitHub Actions Artifacts**, and delivered straight to your **Telegram** bot/channel.

---

## 🏗️ Architecture Overview

```text
GitHub Actions (Ubuntu Runner)
      │
      ▼
Docker Container (Xvfb 1920x1080 Display :99)
      │
      ├──► XFCE Desktop (xfwm4 window manager)
      │       │
      │       ▼
      ├──► Chromium Browser (Playwright automation + manual interaction)
      │
      ├──► PulseAudio (VirtualSpeaker null-sink + VirtualMic WebRTC device)
      │       │
      │       ▼
      ├──► FFmpeg 1080p Recorder (x11grab @ 30fps + PulseAudio monitor)
      │       │
      │       ▼
      │    MP4 Output -> GitHub Artifacts & Telegram Upload
      │
      └──► TigerVNC (127.0.0.1:5900) ──► noVNC Web (127.0.0.1:6080)
                                                ▲
                                                │
Mobile / Laptop Browser ──► Cloudflare Access ──┴──► Cloudflare Named Tunnel
```

---

## 📁 Project Structure

```text
zoom-recorder/
├── .github/
│   └── workflows/
│       ├── build-image.yml       # Builds & caches Docker image to ghcr.io
│       └── record-class.yml      # Daily scheduled + manual recording workflow
├── chromium-policy/
│   └── zoom.json                 # Auto-grants media permissions & suppresses popups
├── desktop/
│   ├── start-desktop.sh          # Initializes Xvfb 1920x1080 & XFCE window manager
│   ├── start-audio.sh            # Sets up PulseAudio VirtualSpeaker & VirtualMic
│   └── start-vnc.sh              # Starts x11vnc & noVNC web server (port 6080)
├── scripts/
│   ├── entrypoint.sh             # Container startup orchestrator
│   ├── health-check.sh           # Verifies display, audio, and VNC health
│   └── cleanup.sh                # Graceful service shutdown handler
├── Dockerfile                    # Container definition (Ubuntu Noble + Playwright + XFCE)
├── requirements.txt              # Python runtime dependencies
├── recorder.py                   # Automation engine, meeting state machine & FFmpeg recorder
├── popup_guard.js                # Clean in-page cookie & notice dismisser
├── .env.example                  # Template for local environment variables
├── .gitignore                    # Prevents credentials & recordings from entering git
└── README.md                     # Comprehensive setup and usage guide
```

---

## 🚀 Complete Step-by-Step Setup Guide

### Step 1: Create the GitHub Repository

1. Log in to [GitHub](https://github.com).
2. Click the **+** icon in the top-right corner -> **New repository**.
3. Name your repository (e.g., `naresh-zoom-recorder`).
4. Choose **Public** (recommended for unlimited free GitHub Actions minutes) or **Private**.
5. Do not initialize with README or license. Click **Create repository**.
6. Clone or push this project code to your repository:
   ```bash
   git remote add origin https://github.com/<YOUR_USERNAME>/naresh-zoom-recorder.git
   git branch -M main
   git push -u origin main
   ```
7. Enable workflow permissions:
   - Go to **Settings** -> **Actions** -> **General**.
   - Under **Workflow permissions**, select **Read and write permissions**.
   - Check **Allow GitHub Actions to create and approve pull requests**.
   - Click **Save**.

---

### Step 2: Configure Telegram Bot & Chat

1. **Create Bot Token (`TG_BOT_TOKEN`):**
   - Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
   - Send `/newbot`, enter a name and unique username (e.g., `MyClassRecorderBot`).
   - Copy the HTTP API token provided (format: `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`).

2. **Obtain Telegram API Credentials (`TG_API_ID` & `TG_API_HASH`):**
   - Visit [my.telegram.org](https://my.telegram.org) and log in with your phone number.
   - Click **API development tools**.
   - Create an application (app title: `Recorder`, short name: `rec`).
   - Copy your numerical **App api_id** and hexadecimal **App api_hash**.

3. **Get Your Telegram Chat ID (`TG_CHAT_ID`):**
   - Search for **[@userinfobot](https://t.me/userinfobot)** on Telegram and press `/start`.
   - Copy the numerical **Id** (e.g., `987654321`).
   - Open a chat with your newly created bot and click **Start** so it has permission to message you.
   - *(Optional Channel Setup)*: Create a private Telegram channel, add your bot as an **Administrator**, and use the channel ID (starts with `-100...`).

---

### Step 3: Create a Free Cloudflare Named Tunnel

Cloudflare Named Tunnels allow you to securely expose the container's noVNC web interface (port 6080) to the internet without opening router ports or running permanent servers.

1. **Sign up / Log in to Cloudflare:**
   - Go to the [Cloudflare Dashboard](https://dash.cloudflare.com/) (free tier).
   - Ensure you have an active domain on Cloudflare (even a free domain or cheap `.xyz`/`.top` works).
2. **Access Cloudflare Zero Trust:**
   - In the left sidebar, click **Zero Trust**.
   - Go to **Networks** -> **Tunnels** -> Click **Create a tunnel**.
3. **Select Cloudflared:**
   - Choose **Cloudflared** as the connector type -> Click **Next**.
   - Name your tunnel (e.g., `github-zoom-vnc`) -> Click **Save tunnel**.
4. **Copy the Tunnel Token (`CF_TUNNEL_TOKEN`):**
   - Under **Install and run a connector**, look at the command box under **Docker** or **Debian 64-bit**.
   - Copy the token string following `--token` (it begins with `eyJhIjoi...`).
   - Save this token; you will paste it into GitHub Secrets as `CF_TUNNEL_TOKEN`.
5. **Configure Public Hostname:**
   - In the tunnel configuration, navigate to the **Public Hostnames** tab -> Click **Add a public hostname**.
   - **Subdomain:** `vnc` (or any name you choose)
   - **Domain:** Select your domain from the dropdown (e.g., `yourdomain.com`).
   - **Service Type:** `HTTP`
   - **URL / Service:** `localhost:6080` (or `127.0.0.1:6080`)
   - Click **Save hostname**.
   - Your public VNC endpoint is now: `https://vnc.yourdomain.com`.

---

### Step 4: Configure Cloudflare Access Authentication

Protect your VNC web interface with email PIN authentication so unauthorized visitors cannot reach it.

1. In the **Zero Trust Dashboard**, go to **Access** -> **Applications** -> Click **Add an application**.
2. Select **Self-hosted**.
3. **Application Configuration:**
   - **Application name:** `Remote Desktop Access`
   - **Application domain:** `vnc.yourdomain.com` (matching your public hostname from Step 3).
4. **Add an Access Policy:**
   - **Policy name:** `Allow Me`
   - **Action:** `Allow`
   - Under **Configure rules**:
     - **Selector:** `Emails`
     - **Value:** Enter your personal email address.
5. Click **Next** -> Click **Save application**.
6. When you open `https://vnc.yourdomain.com`, Cloudflare will ask for your email and send a one-time 6-digit login PIN.

---

### Step 5: Configure GitHub Actions Secrets & Variables

In your GitHub repository, navigate to **Settings** -> **Secrets and variables** -> **Actions**.

#### Repository Secrets (Click "New repository secret")

| Secret Name | Value Description | Example |
| :--- | :--- | :--- |
| `PORTAL_URL` | Portal website login URL | `https://classes.nareshit.com` |
| `PORTAL_USER` | Your student portal username or email | `8074900000` |
| `PORTAL_PASS` | Your student portal password | `MyPassword123` |
| `TG_BOT_TOKEN` | Telegram Bot token from @BotFather | `712345678:AAH...` |
| `TG_API_ID` | Telegram API App ID from my.telegram.org | `2048590` |
| `TG_API_HASH` | Telegram API Hash from my.telegram.org | `4f3a7c8e9...` |
| `TG_CHAT_ID` | Your Telegram User ID or Channel ID | `987654321` |
| `CF_TUNNEL_TOKEN` | Cloudflare Named Tunnel token from Step 3 | `eyJhIjoi...` |
| `VNC_PASSWORD` | Strong password for noVNC web login | `SecretVncPass2026!` |
| `ZOOM_EMAIL` | *(Optional)* Zoom account email if forced to sign in | `student@gmail.com` |
| `ZOOM_PASS` | *(Optional)* Zoom account password | `ZoomPass123` |
| `ZOOM_COOKIES` | *(Optional)* Exported Zoom session cookies (bypasses bot prompts) | `.zoom.us ...` |

#### Repository Variables (Click the "Variables" tab -> "New repository variable")

| Variable Name | Default Value | Description |
| :--- | :--- | :--- |
| `VNC_PUBLIC_URL` | `https://vnc.yourdomain.com` | Full HTTPS URL configured in Cloudflare Tunnel (Step 3). Displayed in logs, Telegram notifications, and Step Summary. |
| `CLASS_KEYWORD` | `React JS` | Text or pattern on portal class card (e.g. `React JS` or `Advance Java\|Advanced Java`). |
| `CLASS_START` | `19:30` | Class start time in IST (24-hour format: `HH:MM`). |
| `CLASS_DURATION_MIN`| `90` | Expected class duration in minutes. |
| `MANUAL_JOIN_WAIT_SEC`| `600` | Seconds (default 10 min) to wait for human interaction when Zoom requires verification. |
| `BLOCK_TRACKERS` | `1` | Set to `0` if portal scripts fail to load. |

---

### Step 6: Build the Docker Image

1. Pushing commits to `main` triggers the **Build recorder image** workflow (`.github/workflows/build-image.yml`).
2. Navigate to the **Actions** tab in your repository.
3. Click on **Build recorder image** and wait for the green checkmark (`✅`).
4. The image will be securely published to GitHub Container Registry (`ghcr.io/<your_username>/naresh-zoom-recorder:latest`).

---

### Step 7: Start the Recording Workflow

You can trigger a recording run manually anytime or rely on the automated schedule.

#### Manual Trigger (Mobile or Laptop)
1. Open your repository on GitHub.
2. Go to **Actions** -> Click **Record class** in the left menu.
3. Click **Run workflow**:
   - `class_keyword`: e.g. `React JS` or `Advance Java` (leave blank to use variable).
   - `class_start`: e.g. `now` for immediate test, or `19:30`.
   - `class_end`: *(optional)* hard stop time.
   - `class_duration_min`: e.g. `90`.
   - `manual_join_wait_sec`: e.g. `600`.
4. Click the green **Run workflow** button.

---

### Step 8: Open Remote Desktop from Android / iOS / Laptop

1. Once the workflow starts, check your **Telegram** or the **GitHub Job Summary**:
   - Telegram sends:
     ```text
     [20:00:15] 🌐 [Portal Login] Entering credentials...
     [20:00:30] ⚠️ Action Required: Bot verification challenge presented by Zoom!
     🖥️ Remote Desktop: https://vnc.yourdomain.com
     ```
2. Tap the link **`https://vnc.yourdomain.com`** directly in your mobile browser (Chrome, Safari, Brave).
3. **Cloudflare Access** prompts for your email:
   - Enter your email address.
   - Enter the 6-digit PIN sent to your inbox.
4. **noVNC Web Interface** loads:
   - Click **Connect**.
   - Enter your `VNC_PASSWORD`.
5. You now see the real 1080p Linux desktop with Google Chrome running live!

---

### Step 9: Interact with Zoom via noVNC

- **Touch Controls on Phone:**
  - Tap anywhere to click.
  - Drag with one finger to move the mouse cursor.
  - Two-finger pinch to zoom in/out on small buttons.
  - Tap the keyboard icon in the noVNC side slider to type names or passcodes.
- **Completing Prompts:**
  - If Zoom asks for your name: type your name.
  - If Zoom shows a reCAPTCHA challenge or checkbox: tap to solve it.
  - If Zoom shows *"Sign in to join"*: click and sign in normally.
  - Click the blue **Join** button.
- **Automatic Handoff:**
  - The moment you are admitted into the meeting, the recorder automatically detects meeting admission!
  - It logs: `✅ [Meeting State] Confirmed inside Zoom meeting! Starting 1080p recording...`
  - It notifies Telegram: `✅ Confirmed inside Zoom meeting - starting 1080p recording`.
  - FFmpeg starts capturing the 1080p video and PulseAudio meeting audio.
  - You can close your mobile browser; the cloud runner continues recording autonomously!

---

### Step 10: Download Completed Recordings

When the meeting concludes (host ends meeting or maximum duration is reached):

1. **GitHub Actions Artifacts:**
   - In GitHub, open the workflow run.
   - Scroll to the bottom to the **Artifacts** section.
   - Download **`class-recording`** (contains the full 1080p `class_recording.mp4` file).
   - Download **`recording-diagnostics`** (contains execution markdown summaries and FFmpeg logs).
2. **Telegram Delivery:**
   - The bot automatically remuxes with faststart streaming headers and uploads the video directly to your Telegram chat or channel.
   - Videos exceeding Telegram's 2 GB limit are automatically split into seamless numbered parts.

---

## 🛠️ Troubleshooting & Diagnostic Guide

| Issue | Root Cause | Solution |
| :--- | :--- | :--- |
| **noVNC screen is black or won't connect** | Cloudflare Tunnel is not running or tunnel token is missing. | Verify `CF_TUNNEL_TOKEN` secret matches your Cloudflare Zero Trust tunnel token. |
| **Cloudflare Access denies login** | Email address not in Access Policy. | Add your email to the Cloudflare Access Application policy in Cloudflare Zero Trust dashboard. |
| **"Automated bots aren't allowed to join"** | Zoom's guest bot check flagged the datacenter IP. | Open `https://vnc.yourdomain.com` on your mobile browser, click "Sign in to join" or complete the prompt manually. Alternatively, supply `ZOOM_COOKIES`. |
| **Portal login failed** | Wrong credentials or modified portal login input selectors. | Double-check `PORTAL_USER` and `PORTAL_PASS`. Set variable `USER_SELECTOR` / `PASS_SELECTOR` if the portal form layout changed. |
| **Join button not found** | `CLASS_KEYWORD` does not match the live card text. | Check the portal live card title (e.g., use `Advance Java\|Advanced Java` or `React JS`). |
| **Silent audio in recording** | PulseAudio null-sink was not attached. | PulseAudio automatically binds `VirtualSpeaker.monitor` to FFmpeg. Check `/tmp/ffmpeg.log` in artifacts. |
| **Workflow cancelled after 6 hours** | GitHub Actions standard runner timeout. | Classes are limited to `CLASS_DURATION_MIN` (default 90m). Adjust duration inputs as needed. |

---

## 🔒 Security Best Practices

1. **Localhost Binding:** `x11vnc` and `noVNC` bind strictly to `127.0.0.1`. No ports are directly reachable over the public internet.
2. **Two-Factor Access:** Remote access requires Cloudflare Access email verification + VNC password.
3. **Secret Masking:** GitHub Actions automatically masks all secrets in workflow output logs.
4. **Ephemeral Containers:** All containers and tunnels are automatically terminated at the conclusion of each recording session.
