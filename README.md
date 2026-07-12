# VelvetOverride

Automated LinkedIn job application bot with AI-powered form filling and resume tailoring.

VelvetOverride searches LinkedIn for target roles, navigates Easy Apply forms, answers
questions using a hybrid config + AI approach (OpenAI/ChatGPT by default, Claude optional),
generates per-job tailored resumes, and tracks every application in a local SQLite database
with a localhost web dashboard for human review.

## ⚡ First-time setup (5 minutes)

```bash
# 1. Install
pip install -e ".[dev]"
patchright install chrome

# 2. Credentials (this file is gitignored)
cp config/.env.example config/.env
#   Edit config/.env: add LINKEDIN_EMAIL and OPENAI_API_KEY.
#   Using Google/SSO to sign in to LinkedIn? Leave LINKEDIN_PASSWORD blank —
#   the bot opens Chrome on the first run and waits for you to sign in by hand,
#   then remembers the session.

# 3. YOUR profile (personal data stays out of git — the *.local.yaml files are gitignored)
cp config/profile.yaml config/profile.local.yaml
#   Edit config/profile.local.yaml with your name, contact, experience, skills.
#   (The bot REFUSES to run while it still says "Jane Doe".)

# 4. Choose how your resume is submitted — pick ONE, in config/settings.yaml:
#    (a) Bring your OWN resume file (simplest):
#          resume:
#            mode: "static"
#            static_resume_path: "C:/path/to/your_resume.pdf"   # PDF/DOC/DOCX
#    (b) Let the bot generate a tailored PDF per job (default):
#          resume:
#            mode: "tailored"

# 5. Pick target roles + locations in config/settings.yaml (search.keywords /
#    search.locations), or override on the CLI with -k / -l.

# 6. Try it safely, then go live, then watch it
velvetoverride run --dry-run          # fills forms but does NOT submit
velvetoverride run --live             # submit applications
velvetoverride dashboard              # http://127.0.0.1:5000  (progress + config + fit)
```

> **Full setup details** are in the [Setup](#setup) section below. **First live run:**
> watch the Chrome window that opens and complete any LinkedIn login / CAPTCHA / 2FA once —
> the session is remembered after that.

## Features

- **7-type form field detection** — text, numeric, radio, dropdown, checkbox, file upload, textarea
- **Hybrid field solver** — learned answers → config → profile → location/EEO/consent → LLM fallback
- **OpenAI (ChatGPT) by default** — provider-agnostic LLM layer; tuned for OpenAI's free daily token-sharing bucket
- **ChatGPT experience-fit check** — honest "am I really a lead?" verdict per job (seniority, gaps, apply/skip), judged from your *real* titles and dates; optional gate
- **Per-job resume tailoring** — keyword-aligned summary + per-role technologies; bullets stay verbatim. Or **bring your own resume** (`resume.mode: static`)
- **Beyond Easy Apply** — optionally follows non-Easy-Apply jobs to the company ATS (Workday/Greenhouse/Lever) and fills them with the LLM (`external_apply` section)
- **Robust de-duplication** — never re-applies to a job by canonical LinkedIn **Job ID**, normalized URL, or fuzzy title+company; failed jobs stay retryable
- **Multi-role × multi-location search** — each role and location searched separately and merged, full-list scroll + pagination
- **24-hour recency** — applies to fresh postings, sorted newest-first
- **Submit verification** — confirms the application actually went through before recording it as applied
- **Job match scoring + salary band filter** — re-checked against the full JD before applying
- **Anti-detection** — Patchright (undetected Playwright), persistent Chrome sessions, human-like timing
- **CAPTCHA / SSO login** — manual (default), 2Captcha, or CapSolver; Google/SSO logins persist after a one-time manual sign-in
- **Localhost dashboard** — Flask web UI: applications, experience-fit, **config & searched roles**, runs, errors, clickable resumes
- **Reusable by anyone** — personal data in gitignored `*.local.yaml`; a placeholder guard prevents applying with template data
- **Scheduling** — one-command Windows Task Scheduler setup (every 5 days at 9 AM PST by default)
- **Dry-run mode** — fills forms without submitting for safe calibration

## Setup

### 1. Install

```bash
git clone https://github.com/<you>/VelvetOverride.git && cd VelvetOverride
pip install -e ".[dev]"      # Python 3.11+
patchright install chrome    # real Chrome for stealth
```

### 2. Secrets (`config/.env` — gitignored)

```bash
cp config/.env.example config/.env
```

Edit `config/.env`:

```ini
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=your_password        # (only used if not signing in via Google SSO)
OPENAI_API_KEY=sk-proj-...             # get one at https://platform.openai.com/api-keys
```

> If your LinkedIn uses **Google/SSO**, leave the password blank — on the first run the
> bot opens Chrome and waits for you to sign in manually. The session then persists
> (a real Chrome profile in `browser_data/`), so later runs log in automatically.

### 3. Your profile & answers (kept OUT of git)

Personal data lives in `*.local.yaml` files, which are **gitignored**. The committed
`profile.yaml` / `answers.yaml` are safe placeholders. Copy and edit the local copies:

```bash
cp config/profile.yaml config/profile.local.yaml    # your experience, skills, projects
cp config/answers.yaml config/answers.local.yaml    # your predetermined form answers
```

Edit `config/profile.local.yaml` — name, contact, experience (each bullet tagged with
`skills:`), education, projects, and `technology_experience` (years per tech). The loader
automatically prefers `*.local.yaml` when present.

### 4. Target roles & locations (`config/settings.yaml`)

Set `search.keywords` (each role is searched separately) and `search.locations`, or pass
them on the CLI (see below). Tune `bot.max_applications`, salary band, and the `fit:` block.

### 5. Estimate token use, then run

```bash
velvetoverride token-test -n 10   # measures tokens/job against sample JDs (no LinkedIn)

velvetoverride run --dry-run      # fills forms but does NOT submit — calibrate first
velvetoverride run --live         # submit applications

velvetoverride dashboard          # → http://127.0.0.1:5000  (progress + fit + errors)
```

> ⚠️ **First live run:** watch the Chrome window that opens and complete any LinkedIn
> login / CAPTCHA / 2FA. After that the session is remembered.

### Free token sharing (ChatGPT)

OpenAI grants **complimentary daily tokens** on API traffic you share with them, on
eligible models. This bot is tuned to fit inside that free bucket:

- High-volume field Q&A uses a **mini model** (`gpt-4.1-mini`) — ~2.5M free tokens/day (tier 1-2).
- Resume tailoring uses `gpt-4.1` — ~250K free tokens/day.
- At 5 applications/day the bot uses **~10K tokens/day** — about **0.4%** of the free mini bucket.

To activate: be Usage tier 1+, then enable sharing at
<https://platform.openai.com/settings/organization/data-controls/sharing>.

## CLI Commands

```
velvetoverride run [OPTIONS]                  Run the application bot
  --dry-run / --live                          Override dry_run setting
  --max-apps N                                Max applications this run (overrides settings.yaml)
  --min-salary N / --max-salary N             Salary band filter (annual)
  -k, --keyword "Role"                        Target role to search (repeatable; overrides config)
  -l, --location "City/Remote"                Location to search (repeatable; overrides config)
  --loop [--loop-delay N] [--max-loops N]     Re-run continuously until stopped
  -v                                          Verbose/debug logging

velvetoverride token-test [-n N]              Estimate token use on N sample jobs (no LinkedIn)
velvetoverride dashboard [--port P]           Launch the localhost tracking dashboard
velvetoverride stats                          Show application statistics (incl. tokens/errors)
velvetoverride runs [--limit N]               Show recent bot run history
velvetoverride errors [--limit N]             Show recorded errors from runs
velvetoverride export [--format csv|json]     Export tracking data
velvetoverride review [--approve]             Review / approve LLM-answered questions
velvetoverride tailor TITLE COMPANY JD_FILE   Generate a tailored resume (no apply)
```

### Scheduled run (Windows, every 5 days at 9 AM PST)

```powershell
# Registers a per-user task at the local equivalent of 9:00 AM Pacific, every 5 days
powershell -ExecutionPolicy Bypass -File scripts\register_schedule.ps1
# Custom cadence (e.g. daily):  ... register_schedule.ps1 -DaysInterval 1

# Test it immediately
Start-ScheduledTask -TaskName "VelvetOverride"
```

The task runs `scripts\run_daily.ps1`, which respects `settings.yaml` (dry-run stays on
until you flip `bot.dry_run: false`) and logs to `data/logs/`.

### Examples

```bash
# Apply to max 10 jobs paying $120K-$200K
velvetoverride run --live --max-apps 10 --min-salary 120000 --max-salary 200000

# Override target roles + locations from the CLI (repeat the flags)
velvetoverride run --live -k "AI Engineer" -k "ML Engineer" -l "Seattle" -l "Remote"

# Run continuously, re-checking for fresh postings every 30 min
velvetoverride run --live --loop --loop-delay 1800

# Dry-run with everything from settings.yaml
velvetoverride run --dry-run
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
OPENAI_API_KEY=sk-proj-your-key-here
```

Then customize your profile and preferences. **Put personal data in the
gitignored `*.local.yaml` copies**, never the committed templates:
```bash
cp config/profile.yaml config/profile.local.yaml   # your experience, skills, projects
cp config/answers.yaml config/answers.local.yaml    # your predetermined answers
nano config/profile.local.yaml   # real name, contact, experience
nano config/settings.yaml         # target roles, locations, filters (or settings.local.yaml)
```

The loader always prefers `*.local.yaml` over the committed placeholder, and the
bot **refuses to run** while the profile still looks like the "Jane Doe"
placeholder — so you can't accidentally apply with template data.

#### 5. Run tests to verify installation

```bash
pytest tests/ -v
# All 172 tests should pass
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
| `bot.max_form_steps` / `max_stuck_retries` | Easy Apply walker limits before giving up |
| `search.keywords` | Roles to search — each is a separate search, merged + de-duped |
| `search.locations` | Locations — each searched separately (crossed with keywords) |
| `search.easy_apply_only` | `true` = Easy Apply only; `false` to also surface external-ATS jobs |
| `search.date_posted` | `any` / `past_month` / `past_week` / `past_24h` |
| `search.sort_by` | `date` (newest first) or `relevance` |
| `search.max_pages` | Result pages to walk per keyword (default: 5) |
| `search.job_types` | full_time, part_time, contract, temporary, internship |
| `search.experience_levels` | internship, entry_level, associate, mid_senior, director, executive |
| `search.remote` | on_site, remote, hybrid |
| `search.blacklist_companies` / `blacklist_keywords` | Companies / JD words that trigger skip |
| `search.min_match_score` | Minimum job-profile match score (0-100), re-checked on the full JD |
| **`external_apply.enabled`** | Follow non-Easy-Apply jobs to the company ATS and fill them |
| **`external_apply.submit`** | `false` = fill but stop before final submit (safer); `true` = submit |
| **`external_apply.max_pages`** | Max ATS pages to walk before giving up (default: 8) |
| **`fit.enabled`** | Run the ChatGPT experience-fit check on each job |
| **`fit.min_score`** | Skip jobs scoring below this (0 = record only, don't gate) |
| **`fit.skip_recommendations`** | e.g. `["skip"]` to skip roles it deems a clear mismatch |
| `salary.min_annual` / `max_annual` | Salary band (null = no limit) |
| `captcha.strategy` / `api_key` / `timeout` | `manual`/`2captcha`/`capsolver`; timeout also covers manual login |
| `browser.channel` / `headless` / `keep_open` | Real Chrome; headed for stealth; keep window open after run |
| `llm.provider` / `field_model` / `resume_model` | `openai` (default) or `anthropic`; per-task models |
| `llm.fit_model` | Model for the experience-fit judgement (default: field model) |
| **`resume.mode`** | `tailored` (per-job PDF) or `static` (upload your own file) |
| **`resume.static_resume_path`** | Path to your own PDF/DOCX (static mode, or tailoring fallback) |
| `resume.reorder_bullets` / `max_bullets_per_job` | Bullet ordering/trimming (text always verbatim) |
| `resume.target_keyword_coverage` | ATS keyword target (default: 0.70 = 70%) |

Personal data belongs in gitignored `config/profile.local.yaml` / `answers.local.yaml`
/ `settings.local.yaml` — the loader prefers these over the committed placeholders.

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
| **OpenAI API** (measured, `token-test`) | ~2,100 tokens/job ≈ $0.002/job at billed rates |
| **Typical day** (5 apps) | ~10K tokens ≈ **$0** if free token-sharing is enabled |
| **Monthly** (5 apps/day) | ~314K tokens — well inside the ~2.5M/day free mini bucket |

To minimize costs:
- **Enable free token sharing** (see above) — at this volume the LLM is effectively free
- Fill out `answers.yaml` thoroughly — the more questions handled by config, the fewer API calls
- The **answer memory system** reduces API costs over time as it learns
- Use **spot instances** (EC2) or **preemptible VMs** (GCP) for ~70% savings on cloud hosting

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `patchright install chrome` fails | Install Chrome manually first: `wget` + `dpkg` (see above) |
| WeasyPrint import error | Install system deps: `apt install libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0` |
| "Looks like you launched a headed browser without having a XServer running" | Start Xvfb: `Xvfb :99 -screen 0 1920x1080x24 & export DISPLAY=:99` |
| LinkedIn security checkpoint / CAPTCHA | Set `captcha.strategy` in settings.yaml. `manual` (default) waits for you to solve via VNC. `2captcha` or `capsolver` auto-solve via API. Set `CAPTCHA_API_KEY` in `.env` for API strategies. |
| `OPENAI_API_KEY` not set | Add it to `config/.env`. The bot works without it but can't handle unknown questions or tailor resumes. |
| Free daily tokens not applying | Enable data sharing at platform.openai.com data controls, and confirm you're Usage tier 1+. Only eligible models (gpt-4.1/-mini, gpt-4o/-mini, gpt-5 family, o-series) qualify. |
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
