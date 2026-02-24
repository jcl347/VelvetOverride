# VelvetOverride

Automated LinkedIn job application bot with AI-powered form filling and resume tailoring.

VelvetOverride searches LinkedIn for target roles, navigates Easy Apply forms, answers
questions using a hybrid config + Claude AI approach, generates per-job tailored resumes,
and tracks every application in a local SQLite database for human review.

## Features

- **7-type form field detection** — text, numeric, radio, dropdown, checkbox, file upload, textarea
- **5-tier hybrid field solver** — learned answers → YAML config → profile data → EEO handler → Claude LLM
- **Per-job resume tailoring** — keyword extraction from JDs, bullet point ranking, summary rewriting, ATS scoring
- **Answer memory** — learns from human corrections, converges toward zero API cost over time
- **Job match scoring** — applies to best-fit jobs first by scoring JDs against your skill profile
- **Fuzzy duplicate detection** — catches reposted jobs with slightly different titles
- **Anti-detection** — Patchright (undetected Playwright), persistent Chrome sessions, human-like timing, activity diversification
- **Dry-run mode** — fills forms without submitting for safe calibration
- **Full tracking** — SQLite database with CSV/JSON export and a human review queue

## Quick Start (Local)

```bash
# Install
pip install -e ".[dev]"
patchright install chrome

# Configure
cp config/.env.example config/.env
# Edit config/.env — add LinkedIn credentials + Anthropic API key
# Edit config/profile.yaml — your resume data
# Edit config/answers.yaml — your predetermined answers
# Edit config/settings.yaml — target roles, locations, filters

# Run
velvetoverride run --dry-run    # Test without submitting
velvetoverride run --live       # Submit applications
```

## CLI Commands

```
velvetoverride run [--dry-run|--live] [-v]   Run the application bot
velvetoverride stats                          Show application statistics
velvetoverride export [--format csv|json]     Export tracking data
velvetoverride review                         Show LLM-answered questions needing review
velvetoverride tailor TITLE COMPANY JD_FILE   Generate a tailored resume (no apply)
```

---

## Deployment Guide

This section covers two production deployment options. Both assume you are starting
from a fresh Ubuntu 22.04 or 24.04 server.

### System Requirements

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPUs | 2+ vCPUs |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 30 GB |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Python | 3.11+ | 3.11+ |
| Network | Outbound HTTPS | Outbound HTTPS |

### Common Setup (Both Options)

Run these steps on your server regardless of which deployment option you choose.

#### 1. System packages

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Python 3.11+ (Ubuntu 24.04 ships 3.12; for 22.04 use deadsnakes)
# On Ubuntu 22.04:
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
# On Ubuntu 24.04: python3 is already 3.12+, skip the above

# WeasyPrint system dependencies (for PDF resume generation)
sudo apt install -y \
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    libffi-dev

# Build tools and git
sudo apt install -y build-essential git curl wget
```

#### 2. Install Google Chrome

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg --install google-chrome-stable_current_amd64.deb
sudo apt install --fix-broken -y
rm google-chrome-stable_current_amd64.deb

# Verify
google-chrome --version
```

#### 3. Clone and install VelvetOverride

```bash
git clone <your-repo-url> ~/VelvetOverride
cd ~/VelvetOverride

# Create virtual environment
python3.11 -m venv .venv   # or python3 -m venv .venv on 24.04
source .venv/bin/activate

# Install the project
pip install -e ".[dev]"

# Install Chrome driver for Patchright
patchright install chrome
```

#### 4. Configure

```bash
# Create your secrets file
cp config/.env.example config/.env

# Edit with your credentials
nano config/.env
```

Set these values in `config/.env`:
```
LINKEDIN_EMAIL=your_real_email@example.com
LINKEDIN_PASSWORD=your_real_password
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Then customize your profile and preferences:
```bash
nano config/profile.yaml     # Your real experience, skills, education
nano config/answers.yaml      # Your predetermined answers (work auth, etc.)
nano config/settings.yaml     # Target job roles, locations, filters
```

#### 5. Run tests to verify installation

```bash
pytest tests/ -v
# All 37 tests should pass
```

---

### Option A: Cloud VM with Desktop (VNC/RDP)

Best for: watching the bot work, manual intervention when needed, initial calibration.

You can see the browser, interact with CAPTCHAs, and observe form filling in real time.

#### AWS EC2

##### Launch the instance

1. Go to [EC2 Console](https://console.aws.amazon.com/ec2/) → **Launch Instance**
2. Settings:
   - **Name**: `velvetoverride-bot`
   - **AMI**: Ubuntu 24.04 LTS (or search AWS Marketplace for "Playwright on Ubuntu with GUI" for a pre-configured option)
   - **Instance type**: `t3.large` (2 vCPUs, 8 GB RAM) — recommended; `t3.medium` (4 GB) works for light use
   - **Key pair**: Create or select an existing SSH key
   - **Storage**: 25 GB gp3
   - **Security group**: Allow inbound SSH (port 22) from your IP
3. Click **Launch Instance**

##### Install desktop environment + VNC

```bash
# SSH into your instance
ssh -i your-key.pem ubuntu@<public-ip>

# Install Xfce (lightweight desktop) and VNC server
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y xfce4 xfce4-goodies
sudo apt install -y tightvncserver dbus-x11

# Set VNC password (you'll be prompted)
vncserver
# Enter a password (6-8 characters) when prompted
# Then kill the initial session so we can configure it
vncserver -kill :1

# Configure VNC to use Xfce
cat > ~/.vnc/xstartup << 'XSTARTUP'
#!/bin/bash
xrdb $HOME/.Xresources
startxfce4 &
XSTARTUP
chmod +x ~/.vnc/xstartup

# Start VNC server at 1920x1080
vncserver -geometry 1920x1080 -depth 24 :1
```

##### Connect via SSH tunnel (secure — no open ports needed)

From your local machine:
```bash
# Create SSH tunnel: forwards your local port 5901 → server's VNC port
ssh -i your-key.pem -L 5901:localhost:5901 ubuntu@<public-ip>

# Now open a VNC client (e.g., RealVNC Viewer, TigerVNC, macOS Screen Sharing)
# Connect to: localhost:5901
# Enter the VNC password you set above
```

You should see the Xfce desktop. Open a terminal within the desktop.

##### Run VelvetOverride

Inside the VNC desktop terminal:
```bash
cd ~/VelvetOverride
source .venv/bin/activate

# Dry-run first — watch the browser open and fill forms
velvetoverride run --dry-run -v

# When satisfied, run live
velvetoverride run --live -v
```

The Chrome browser will open visually on the VNC desktop. You can watch it navigate
LinkedIn, fill out forms, and upload resumes in real time.

#### GCP Compute Engine

##### Launch the instance

1. Go to [Compute Engine Console](https://console.cloud.google.com/compute) → **Create Instance**
2. Settings:
   - **Name**: `velvetoverride-bot`
   - **Region**: Choose one close to you
   - **Machine type**: `e2-standard-2` (2 vCPUs, 8 GB RAM)
   - **Boot disk**: Ubuntu 24.04 LTS, 25 GB SSD
   - **Firewall**: Allow HTTP/HTTPS (for Chrome Remote Desktop)
3. Click **Create**

##### Install Chrome Remote Desktop (recommended by Google)

```bash
# SSH into your instance (use the GCP Console SSH button or gcloud)
gcloud compute ssh velvetoverride-bot

# Install Chrome Remote Desktop
sudo apt update
curl -L -o /tmp/crd.deb \
    https://dl.google.com/linux/direct/chrome-remote-desktop_current_amd64.deb
sudo DEBIAN_FRONTEND=noninteractive apt install -y /tmp/crd.deb
rm /tmp/crd.deb

# Install Xfce desktop
sudo DEBIAN_FRONTEND=noninteractive apt install -y xfce4 desktop-base dbus-x11

# Configure Chrome Remote Desktop to use Xfce
sudo bash -c 'echo "exec /etc/X11/Xsession /usr/bin/xfce4-session" > /etc/chrome-remote-desktop-session'

# Set up the remote desktop session:
# 1. On your local machine, go to https://remotedesktop.google.com/headless
# 2. Click "Set up via SSH"
# 3. Choose "Begin" → "Next" → "Authorize"
# 4. Copy the Debian Linux command shown
# 5. Paste and run it on your GCP VM
# 6. Set a PIN when prompted (6+ digits)

# Now go to https://remotedesktop.google.com on your local Chrome browser
# Your VM should appear — click it and enter your PIN
```

##### Run VelvetOverride

Inside the Chrome Remote Desktop session, open the Xfce terminal:
```bash
cd ~/VelvetOverride
source .venv/bin/activate
velvetoverride run --dry-run -v
```

---

### Option B: Headless Server (No Desktop)

Best for: unattended scheduled runs, lower resource usage, cron jobs, long-term operation.

No desktop environment, no VNC. The bot runs with a virtual framebuffer (`Xvfb`) that
provides a fake display for Chrome. You control everything over SSH.

#### Setup Xvfb

```bash
# Install Xvfb (virtual framebuffer)
sudo apt install -y xvfb

# Test that it works
Xvfb :99 -screen 0 1920x1080x24 &
export DISPLAY=:99
# Verify Chrome can launch
google-chrome --no-sandbox --disable-gpu --headless=new --screenshot /tmp/test.png https://www.google.com
# If /tmp/test.png exists, Chrome works with Xvfb
kill %1  # Stop the test Xvfb
```

#### Run manually over SSH

```bash
cd ~/VelvetOverride
source .venv/bin/activate

# Start Xvfb and run the bot
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 -ac &

velvetoverride run --dry-run -v

# When done, kill Xvfb
kill %1
```

#### Wrapper script

Create a convenience script to handle Xvfb lifecycle:

```bash
cat > ~/VelvetOverride/run.sh << 'RUNSCRIPT'
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
source .venv/bin/activate

# Start Xvfb if not already running
if ! pgrep -x Xvfb > /dev/null; then
    Xvfb :99 -screen 0 1920x1080x24 -ac &
    XVFB_PID=$!
    sleep 1
fi
export DISPLAY=:99

# Run the bot (pass through all arguments)
velvetoverride "$@"

# Clean up Xvfb if we started it
if [ -n "${XVFB_PID:-}" ]; then
    kill "$XVFB_PID" 2>/dev/null || true
fi
RUNSCRIPT
chmod +x ~/VelvetOverride/run.sh
```

Usage:
```bash
~/VelvetOverride/run.sh run --dry-run -v
~/VelvetOverride/run.sh run --live
~/VelvetOverride/run.sh stats
~/VelvetOverride/run.sh export
```

#### Schedule with cron

```bash
# Open crontab
crontab -e

# Run the bot every weekday at 9 AM (server time), max 25 applications
# Output logged to ~/VelvetOverride/logs/
0 9 * * 1-5 ~/VelvetOverride/run.sh run --live >> ~/VelvetOverride/logs/cron_$(date +\%Y\%m\%d).log 2>&1
```

Create the logs directory:
```bash
mkdir -p ~/VelvetOverride/logs
```

#### Schedule with systemd (alternative to cron)

For better logging, restart behavior, and service management:

```bash
# Create the service unit
sudo tee /etc/systemd/system/velvetoverride.service << 'SERVICE'
[Unit]
Description=VelvetOverride LinkedIn Application Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/VelvetOverride
Environment=DISPLAY=:99
ExecStartPre=/usr/bin/bash -c 'pgrep Xvfb || Xvfb :99 -screen 0 1920x1080x24 -ac &'
ExecStart=/home/ubuntu/VelvetOverride/.venv/bin/velvetoverride run --live
StandardOutput=journal
StandardError=journal
SERVICE

# Create a timer to run it on weekdays at 9 AM
sudo tee /etc/systemd/system/velvetoverride.timer << 'TIMER'
[Unit]
Description=Run VelvetOverride on weekday mornings

[Timer]
OnCalendar=Mon..Fri 09:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
TIMER

# Enable and start the timer
sudo systemctl daemon-reload
sudo systemctl enable velvetoverride.timer
sudo systemctl start velvetoverride.timer

# Check timer status
systemctl list-timers velvetoverride.timer

# View logs
journalctl -u velvetoverride.service -f
```

---

## Configuration Reference

### config/settings.yaml

| Section | Key Settings |
|---|---|
| `bot.dry_run` | `true` = fill forms without submitting (default) |
| `bot.max_applications` | Max applications per session (default: 25) |
| `bot.delay_min` / `delay_max` | Seconds between applications (default: 5-18) |
| `bot.capture_screenshots` | Save screenshots of each form step |
| `search.keywords` | Job titles to search for |
| `search.locations` | Target locations |
| `search.easy_apply_only` | Only show Easy Apply jobs (default: true) |
| `search.experience_levels` | Filter: internship, entry_level, associate, mid_senior, director, executive |
| `search.remote` | Filter: on_site, remote, hybrid |
| `search.blacklist_companies` | Companies to skip |
| `search.blacklist_keywords` | JD keywords that trigger skip |
| `search.min_match_score` | Minimum job-profile match score (0-100) |
| `browser.channel` | `chrome` (real Chrome — stealthier) |
| `browser.headless` | `false` for VNC/desktop; Xvfb handles "headless" |
| `llm.field_model` | Claude model for field Q&A (default: claude-sonnet-4-6) |
| `llm.resume_model` | Claude model for resume tailoring |
| `resume.target_keyword_coverage` | ATS keyword target (default: 0.70 = 70%) |

### config/profile.yaml

Your master resume data. Edit this to match your real background:
- `personal` — name, email, phone, LinkedIn URL, GitHub, location
- `summary` — professional summary (rewritten per job by the LLM)
- `experience` — jobs with bullet points, each tagged with skill keywords
- `education` — degrees, institutions, dates
- `skills` — languages, frameworks, infrastructure, databases, practices
- `technology_experience` — years per technology (used for numeric form fields)

### config/answers.yaml

Predetermined answers for common Easy Apply questions:
- `yes_no` — work authorization, visa, drivers license, relocation, etc.
- `eeo` — always selects "decline to answer" for demographic questions
- `numeric` — years of experience (pulled from `technology_experience`), salary
- `education` — highest degree
- `text_defaults` — LinkedIn URL, portfolio/GitHub
- `learned` — auto-populated as the bot learns from human corrections

---

## Costs

| Component | Cost |
|---|---|
| **EC2 t3.large** (on-demand) | ~$0.083/hr = ~$60/month (if running 24/7) |
| **EC2 t3.large** (spot) | ~$0.025/hr = ~$18/month |
| **GCP e2-standard-2** | ~$0.067/hr = ~$49/month |
| **Claude Sonnet API** (field Q&A) | ~$0.003 per field (most fields use config — free) |
| **Claude Sonnet API** (resume tailoring) | ~$0.01-0.03 per resume |
| **Typical session** (25 apps) | ~$0.50-1.50 in API costs |

To minimize costs:
- Use **spot instances** (EC2) or **preemptible VMs** (GCP) for ~70% savings
- Fill out `answers.yaml` thoroughly — the more questions handled by config, the fewer API calls
- The **answer memory system** reduces API costs over time as it learns
- Run only during business hours and shut down the instance otherwise

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `patchright install chrome` fails | Install Chrome manually first: `wget` + `dpkg` (see above) |
| WeasyPrint import error | Install system deps: `apt install libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0` |
| "Looks like you launched a headed browser without having a XServer running" | Start Xvfb: `Xvfb :99 -screen 0 1920x1080x24 & export DISPLAY=:99` |
| LinkedIn security checkpoint / CAPTCHA | The bot waits 120s for you to solve it manually. Use VNC to intervene, or run `--dry-run` first to establish a session. |
| `ANTHROPIC_API_KEY` not set | Add it to `config/.env`. The bot works without it but can't handle unknown questions or tailor resumes. |
| Chrome crashes with `--no-sandbox` error | Run as non-root user, or add `--no-sandbox` to browser args in `engine.py` |
| VNC black screen | Restart VNC: `vncserver -kill :1 && vncserver -geometry 1920x1080 -depth 24 :1` |

---

## Project Structure

See [CLAUDE.md](CLAUDE.md) for full architecture documentation, research findings,
design decisions, and the innovation breakdown.

## Legal Disclaimer

**This project is for educational and personal use only.** Automated interaction with
LinkedIn may violate their Terms of Service. Users assume all risk. See the full
disclaimer in [CLAUDE.md](CLAUDE.md#legal--ethical-disclaimer).

## License

MIT
