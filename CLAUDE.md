# VelvetOverride — LinkedIn Automated Application Bot

## Project Vision

An agentic bot that mines LinkedIn job listings for target roles, navigates into each
application, identifies required form fields, generates tailored responses (including
dynamically-built resumes), selects pre-determined answers for known question types, and
logs every application for later human review.

---

## Research Findings

### 1. Existing Open-Source LinkedIn Application Bots

| Project | Stack | AI Integration | Key Strengths | Key Weaknesses |
|---|---|---|---|---|
| [Auto_job_applier_linkedIn](https://github.com/GodsScion/Auto_job_applier_linkedIn) (GodsScion) | Python / Selenium / ChromeDriver | OpenAI, Gemini, DeepSeek | Resume tailoring per job, stealth mode, multi-AI provider support, skill extraction from JDs | Selenium-based (fragile selectors), monolithic config |
| [LinkedIn-Easy-Apply-Bot](https://github.com/NathanDuma/LinkedIn-Easy-Apply-Bot) (NathanDuma) | Python / Selenium | None (YAML config) | Comprehensive YAML-driven Q&A mapping, EEO handling, dry-run mode | No AI fallback for unknown questions, breaks on UI changes |
| [EasyApplyJobsBot](https://github.com/wodsuz/EasyApplyJobsBot) (wodsuz) | Python / Selenium | None | Multi-platform (LinkedIn, Glassdoor), CSV/TXT export, unanswered question logging | Selenium-only, no resume customization |
| [LinkedIn-GPT-EasyApplyBot](https://github.com/JorgeFrias/LinkedIn-GPT-EasyApplyBot) (JorgeFrias) | Python / Selenium / OpenAI | GPT-3.5/4 | LLM-powered answer generation for arbitrary questions | Author now discourages use; resume optimization yields better ROI |
| [linkedin-easyapply-using-AI](https://github.com/srikar-kodakandla/linkedin-easyapply-using-AI) | Python / Selenium | GPT-4, Gemini Pro | Multi-model support for form fill | Still Selenium-based |
| [LinkedinEasyApplybot](https://github.com/Eezzeldin/LinkedinEasyApplybot) (Eezzeldin) | Python / Selenium / SQLite | None | SQLite-backed job URL + description storage | Scraper-only; minimal apply logic |
| [job-application-bot-by-ollama-ai](https://github.com/lookr-fyi/job-application-bot-by-ollama-ai) (JobHuntr) | Python / Ollama | Local LLMs | ATS resume threshold, universal website support, fully local AI | Partially paid, complex setup |

**Key takeaways from the community:**
- Selenium dominates, but is brittle — CSS selectors break when LinkedIn updates its UI.
- AI-powered question answering (via LLMs) dramatically improves handling of unexpected fields.
- YAML-based predetermined answer configs are essential as a first-pass before falling back to AI.
- Resume tailoring per job description is the single highest-ROI feature.
- SQLite or structured logging for tracking applications is a common pattern.

### 2. LinkedIn Easy Apply Form Field Types

From the [LinkedIn Talent API](https://learn.microsoft.com/en-us/linkedin/talent/easy-apply/easy-apply) and bot source code analysis:

| Field Type | HTML Pattern | Common Use Cases |
|---|---|---|
| Text Input | `fb-single-line-text__input` | Name, email, phone, LinkedIn URL, website/portfolio |
| Numeric Input | Numeric field | Years of experience, salary expectations |
| Radio Buttons | `MultipleChoiceQuestionDetails` | Work authorization, visa sponsorship, willingness to relocate |
| Dropdown/Select | `<select>` element | Phone country code, gender, veteran status, disability, race |
| Checkbox | `<input type="checkbox">` | Terms acceptance, consent, degree confirmations |
| File Upload | `DocumentQuestionDetails` | Resume (PDF/DOCX, max 2MB), cover letter |
| Textarea | Multi-line text | Cover letter body, additional information, summary |

**Common screening question categories:**
- Legal authorization / visa sponsorship (Yes/No)
- Years of experience with specific technologies (Numeric)
- Willingness to relocate / commute (Yes/No)
- Education level / degree (Dropdown or Checkbox)
- EEO voluntary self-identification: gender, veteran, race, disability (Dropdown — bot should select "Decline to answer")

### 3. Browser Automation Platform Comparison

| Platform | Type | Detection Risk | Adaptability | Cost Model |
|---|---|---|---|---|
| **Selenium + selenium-stealth** | Traditional scripting | Medium-High | Low (selector-bound) | Free |
| **Playwright + playwright-stealth** | Traditional scripting | Medium | Low (selector-bound) | Free |
| **[Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)** | Undetected Playwright fork | Low | Low (selector-bound) | Free |
| **[browser-use](https://github.com/browser-use/browser-use)** | LLM agent on Playwright | Low-Medium | High (LLM-driven) | LLM API costs |
| **[Skyvern](https://github.com/Skyvern-AI/skyvern)** | LLM + CV agent | Low-Medium | Very High | ~$0.10/page |
| **Anthropic Computer Use** | Vision-based agent | Low | Very High | API costs |

**Recommendation: Hybrid approach using Patchright + browser-use**

- **Patchright** as the browser engine — patches `Runtime.enable` CDP leak, removes
  `navigator.webdriver`, handles Closed Shadow DOMs. Currently considered undetectable
  with proper configuration (`launch_persistent_context`, `channel="chrome"`, real
  `user_data_dir`, `headless=False`).
- **browser-use** as the AI agent layer — provides LLM-driven page reasoning, natural
  language task decomposition, and adaptive form filling that survives UI changes.
- Deterministic Playwright selectors for **known, stable elements** (login flow, search
  filters, pagination) to minimize LLM API costs.
- LLM agent fallback for **unknown/unexpected form fields** that don't match the
  predetermined answer config.

### 4. Anti-Detection Strategies

Based on research from [ScrapeOps](https://scrapeops.io/websites/linkedin/), [Scrapfly](https://scrapfly.io/blog/posts/how-to-scrape-linkedin), and [BrightData](https://brightdata.com/blog/how-tos/avoid-bot-detection-with-playwright-stealth):

1. **Browser fingerprint hardening** — Use Patchright with `channel="chrome"` (real
   Chrome, not Chromium), persistent context with real user data directory, no custom
   headers or user-agent overrides.
2. **Human-like timing** — Random delays between 3-15 seconds between actions, natural
   scroll patterns, variable mouse movement paths.
3. **Session management** — Use persistent browser contexts to maintain cookies/sessions
   across runs, avoiding repeated login detection.
4. **Rate limiting** — Cap at 20-30 applications per session, randomized session
   duration, cool-down periods between runs.
5. **Residential proxies** (optional) — Rotating residential IPs if operating at higher
   volume. Datacenter proxies get flagged quickly.
6. **Activity diversification** — Occasionally browse feed, view profiles, check
   notifications between applications to break robotic patterns.

### 5. Resume Tailoring Approach

**Architecture: YAML data + Jinja2 templates + LLM keyword optimization**

Inspired by [pyresume](https://pypi.org/project/pyresume/), [hanula/resume](https://github.com/hanula/resume), and ATS optimization research from [Jobscan](https://www.jobscan.co/) and [Reztune](https://www.reztune.com/):

1. **Master resume in YAML** — All experience, skills, projects, education stored as
   structured data. Each bullet point tagged with skill categories.
2. **Jinja2 LaTeX/HTML templates** — Professional ATS-friendly templates that compile to
   PDF via `pdflatex` or `weasyprint`.
3. **LLM-powered tailoring** — For each job:
   - Extract keywords and required skills from the job description using NLP.
   - Score and rank resume bullet points by relevance to the JD.
   - Rewrite the professional summary to target the specific role.
   - Ensure 60-80% keyword coverage of JD requirements (ATS optimization target).
   - Generate the tailored PDF.
4. **ATS scoring** — Self-score the generated resume against the JD before submission
   (target 80+ on a 100-point scale).

### 6. Application Tracking

Based on patterns from [Eezzeldin/LinkedinEasyApplybot](https://github.com/Eezzeldin/LinkedinEasyApplybot) and the [job-application-management](https://github.com/topics/job-application-management) GitHub topic:

- **SQLite database** for local persistence — lightweight, zero-config, portable.
- Track: company, job title, job URL, job description, date applied, resume version used,
  questions asked, answers given, application status, screening question flags.
- **Export to CSV/JSON** for human review in spreadsheets or other tools.
- **Deduplication** — Skip jobs already applied to via URL matching.
- **Review queue** — Flag applications that required LLM-generated answers for manual
  review before or after submission.

---

## Implementation Architecture

```
VelvetOverride/
├── CLAUDE.md                    # This file — research, architecture, run instructions
├── pyproject.toml               # Project config, dependencies, CLI entry point
├── .gitignore
├── config/
│   ├── settings.yaml            # Bot behavior, search filters, browser, LLM, rate limits
│   ├── profile.yaml             # Master resume data (experience, skills, education)
│   ├── answers.yaml             # Predetermined answers + learned answer memory
│   └── .env.example             # Template for secrets (copy to .env)
├── src/
│   └── velvetoverride/
│       ├── __init__.py
│       ├── main.py              # CLI entry point (click) / orchestrator / bot loop
│       ├── browser/
│       │   ├── engine.py        # Patchright browser launch & lifecycle
│       │   ├── stealth.py       # Human-like behavior (typing, scrolling, diversification)
│       │   └── captcha.py       # CAPTCHA detection + 3-strategy resolver (manual/2captcha/capsolver)
│       ├── linkedin/
│       │   ├── auth.py          # Login, session reuse, checkpoint handling
│       │   ├── search.py        # Job search URL builder, listing scraper, filters
│       │   ├── apply.py         # Multi-step Easy Apply form walker
│       │   └── fields.py        # Form field detection (7 types), label extraction
│       ├── agent/
│       │   ├── llm.py           # Provider-agnostic LLM client (OpenAI default / Anthropic) + token usage
│       │   ├── field_solver.py  # 5-tier hybrid solver (learned→config→profile→EEO→LLM)
│       │   ├── resume_tailor.py # JD analysis → bullet ranking → summary rewrite → PDF (guardrailed)
│       │   ├── token_test.py    # Offline token-usage estimator (sample jobs → real LLM calls)
│       │   └── salary.py        # Salary range extraction (regex) + filtering
│       ├── resume/
│       │   ├── builder.py       # Jinja2 → HTML → PDF (WeasyPrint) pipeline
│       │   ├── scorer.py        # ATS keyword coverage scoring & suggestions
│       │   └── templates/
│       │       └── default.html.j2  # Professional ATS-friendly resume template
│       ├── tracking/
│       │   ├── database.py      # SQLite schema, CRUD, dedup (job_id), errors, tokens, stats
│       │   ├── models.py        # ApplicationRecord, QuestionRecord, JobListing (job_id), enums
│       │   └── export.py        # CSV/JSON/review-queue export
│       ├── web/
│       │   └── dashboard.py     # Flask localhost dashboard (apps, runs, errors, tokens)
│       └── utils/
│           ├── config.py        # YAML config loader, .env secrets, Config dataclass
│           ├── urls.py          # LinkedIn job-ID extraction + URL normalization (dedup)
│           └── logging.py       # structlog setup (console + JSON modes)
├── scripts/
│   ├── run_daily.ps1            # One daily run, logged to data/logs/
│   └── register_schedule.ps1    # Register 9 AM PST Windows scheduled task
├── tests/
│   ├── test_config.py           # Config loading (3 tests)
│   ├── test_field_solver.py     # Hybrid field solver (18 tests)
│   ├── test_resume.py           # Resume builder + ATS scorer (7 tests)
│   ├── test_salary.py           # Salary extraction + filtering (20 tests)
│   ├── test_search.py           # Search URL builder (6 tests)
│   ├── test_tracking.py         # SQLite database + run tracking + approvals (22 tests)
│   ├── test_urls.py             # Job-ID extraction + URL normalization (7 tests)
│   └── test_dedup_and_tracking.py  # job_id dedup, error log, token usage (12 tests)
└── data/                        # (gitignored)
    ├── applications.db          # SQLite tracking database
    ├── logs/                    # Per-run log files
    └── resumes/                 # Generated tailored resume PDFs
```

### Core Flow

```
1. CONFIGURE → Load profile.yaml, answers.yaml, settings.yaml
2. LAUNCH    → Start Patchright persistent browser context (real Chrome)
3. LOGIN     → Authenticate to LinkedIn (session cookie reuse when possible)
4. CAPTCHA   → Detect + resolve any post-login CAPTCHA (manual/2captcha/capsolver)
5. SEARCH    → Navigate to Jobs, apply filters (role, location, Easy Apply, etc.)
6. SALARY    → Extract salary ranges from JDs, filter by configured salary band
7. ITERATE   → For each job listing:
   a. SCRAPE   → Extract job title, company, description, requirements
   b. CHECK    → Deduplicate against tracking DB
   c. TAILOR   → Generate role-specific resume via LLM + Jinja2 pipeline
   d. APPLY    → Click Easy Apply, walk through multi-step form:
      i.   Detect field type (text, radio, dropdown, checkbox, upload, textarea)
      ii.  Lookup answer in answers.yaml config
      iii. If no config match → invoke Claude API for contextual answer
      iv.  Upload tailored resume PDF
      v.   Flag LLM-generated answers for human review
   e. TRACK    → Record application details + salary data in SQLite
   f. DELAY    → Human-like pause before next application
8. EXPORT   → Generate CSV/JSON report of all applications for review
```

### Technology Stack

| Component | Technology | Rationale |
|---|---|---|
| Language | **Python 3.11+** | Ecosystem maturity, all reference bots are Python |
| Package Manager | **uv** | Fast, modern Python package management |
| Browser Engine | **Patchright** (undetected Playwright) | Best-in-class anti-detection, Playwright API compatibility |
| AI Agent Layer | **browser-use** (optional, for complex flows) | LLM-driven adaptive form navigation |
| LLM Provider | **OpenAI (ChatGPT) API** default (`gpt-4.1-mini` for field Q&A, `gpt-4.1` for tailoring); Anthropic optional | Provider-agnostic layer; OpenAI chosen for free daily token-sharing bucket |
| Resume Templates | **Jinja2 + LaTeX** (`pdflatex`) or **Jinja2 + HTML** (`weasyprint`) | Separation of data/layout, ATS-friendly PDF output |
| Resume Data | **YAML** | Human-readable, easy to maintain |
| Database | **SQLite** (via `sqlite3` stdlib) | Zero-config, portable, sufficient for local tracking |
| Web Dashboard | **Flask** | Localhost tracking UI (apps, runs, errors, tokens) |
| Config | **YAML** + **python-dotenv** | Structured config + secure secret management |
| Testing | **pytest** | Standard Python testing |
| Logging | **structlog** | Structured JSON logging for debugging bot runs |

### Key Design Decisions

1. **Hybrid field-solving (config-first, LLM-fallback):** Most Easy Apply questions are
   repetitive across applications (work auth, years of experience, EEO). A YAML config
   handles these deterministically with zero API cost. The LLM is only invoked for
   genuinely novel questions, keeping costs low and speed high.

2. **Patchright over Selenium:** Every major existing bot uses Selenium, and they all
   suffer from detection issues and brittle selectors. Patchright provides the modern
   Playwright API with undetectable browser automation built in.

3. **OpenAI (ChatGPT) by default (Anthropic optional):** The LLM layer is
   provider-agnostic (`llm.provider`). OpenAI is the default because of its free
   daily token-sharing bucket — `gpt-4.1-mini` for high-volume field Q&A / keyword
   scoring (huge free allotment) and `gpt-4.1` for the low-volume resume tailoring.
   Anthropic Claude is a drop-in alternative via the same interface.

4. **Resume-per-application:** Research consistently shows that tailored resumes
   dramatically outperform generic ones. The YAML + Jinja2 pipeline makes this
   computationally cheap while LLM keyword optimization ensures ATS compatibility.

5. **Human-in-the-loop review queue:** Applications with LLM-generated answers are
   flagged for review. The bot can operate in dry-run mode (fill but don't submit) for
   initial calibration.

6. **Persistent browser context:** Reusing Chrome's user data directory across sessions
   avoids repeated login flows and maintains natural browsing history, reducing detection
   risk.

---

## Innovations Beyond Existing Bots

These features are novel relative to the existing open-source landscape:

### 1. Answer Memory System (Self-Improving)
When the bot encounters a novel question and the LLM generates an answer, that
Q&A pair is flagged for human review. Once approved/corrected, it's stored in the
`learned` section of `answers.yaml`. On subsequent runs, the bot uses the learned
answer directly — **zero API cost, zero latency**. Over time, the bot converges
toward needing the LLM less and less as its answer memory grows.

Implementation: `src/velvetoverride/agent/field_solver.py` → `learn_answer()` method
and Tier 1 lookup in `solve()`.

### 2. Job Match Scoring & Prioritization
Before applying, the bot scores each job listing's description against your profile
skills using keyword overlap analysis. Listings are sorted by match score (highest
first), and a configurable `min_match_score` threshold filters out poor-fit roles.
This means the bot applies to your **best-fit jobs first**, maximizing return on
the limited applications-per-session budget.

Implementation: `src/velvetoverride/main.py` → `_score_listings()`.

### 3. Fuzzy Duplicate Detection
Beyond exact URL dedup, the bot uses fuzzy string matching (`thefuzz` / Levenshtein
distance) to detect when the same job has been reposted under a slightly different
title or URL. This prevents wasting applications on duplicates that recruiters or
staffing agencies repost.

Implementation: `src/velvetoverride/main.py` → fuzzy matching in the apply loop.

### 4. Per-Step Screenshot Capture
Every form step is optionally screenshotted and saved with a timestamped filename.
This creates a visual audit trail for debugging form navigation issues and for
human review of what the bot actually did. Essential for calibrating the bot
during dry-run mode.

Implementation: `src/velvetoverride/linkedin/apply.py` → `_maybe_screenshot()`.

### 5. ATS Self-Scoring Before Submission
Before uploading a tailored resume, the bot scores it against the extracted JD
keywords. If keyword coverage is below the target threshold (default 70%), it
can flag the resume for manual improvement. This ensures every submitted resume
has a fighting chance of passing ATS screening.

Implementation: `src/velvetoverride/resume/scorer.py` → `ATSScorer`.

### 6. Activity Diversification (Anti-Pattern Detection)
Between applications, the bot randomly performs non-application LinkedIn actions —
scrolling the feed, checking notifications, or idle pauses. This breaks up the
robotic "search → apply → search → apply" pattern that detection systems flag.

Implementation: `src/velvetoverride/browser/stealth.py` → `diversify_activity()`.

### 7. Tiered Field Resolution (5-Level Cascade)
No other bot has this depth of resolution strategy:
1. **Learned** (human-corrected answers from prior runs)
2. **Config** (predetermined `answers.yaml`)
3. **Profile** (personal info from `profile.yaml`)
4. **EEO handler** (always decline/opt-out)
5. **LLM** (Claude API — flagged for review)

This minimizes API costs while maximizing accuracy.

### 8. Salary Range Extraction & Band Filtering
The bot extracts salary information from job descriptions using regex patterns that
handle multiple formats: `$120,000-$180,000`, `$120K-$150K`, `$55/hr-$75/hr`, and
contextual salary keywords. Hourly rates are automatically annualized (×2,080 hours).
Users configure a target salary band via `--min-salary`/`--max-salary` CLI flags or
`salary.min_annual`/`salary.max_annual` in settings.yaml. Jobs without salary info
are included by default (benefit of the doubt). Salary data is tracked in SQLite
for post-run analysis.

Implementation: `src/velvetoverride/agent/salary.py` → `extract_salary()`,
`salary_in_range()`, `SalaryRange` dataclass. Wired into `main.py` apply loop.

### 9. Multi-Strategy CAPTCHA Resolution
Three configurable strategies for handling LinkedIn security checkpoints:
1. **Manual** (default) — Pauses and polls every 5s for up to 300s, waiting for
   human intervention via VNC/desktop. Emits periodic log reminders.
2. **2Captcha API** — Submits reCAPTCHA sitekey to 2Captcha's human-powered solving
   network (~$1-3 per 1,000 solves). Polls for solution, injects token, triggers callback.
3. **CapSolver API** — AI-powered solving, typically faster (~$0.80-3 per 1,000 solves).
   Same workflow: submit task → poll → inject token.

All strategies fall back to manual if the API solve fails. CAPTCHA detection checks
both URL indicators (`checkpoint`, `challenge`, `captcha`, `security-verification`)
and page elements (iframes, containers, data attributes).

Implementation: `src/velvetoverride/browser/captcha.py` → `detect_captcha()`,
`handle_captcha()`. Integrated into `linkedin/auth.py` login flow and `main.py`
post-login check.

### 10. Run-Level Tracking & History
Every bot execution is tracked as a "run" with full metadata: start/end time,
dry-run vs live mode, salary filters, listings found/filtered, applications
applied/failed/skipped, error messages, and a config snapshot. This enables
trend analysis across runs (e.g., "are my applications getting rejected more
often?"), debugging failed runs without digging through logs, and understanding
how filter changes affect application volume.

Implementation: `src/velvetoverride/tracking/database.py` → `start_run()`,
`finish_run()`, `get_runs()`. Wired into `main.py` orchestrator. CLI via
`velvetoverride runs`.

### 11. Interactive Answer Approval with YAML Persistence
The `velvetoverride review --approve` command provides an interactive CLI workflow
where humans can approve or correct LLM-generated answers. Corrections are
immediately persisted to the `learned` section of `config/answers.yaml`, closing
the feedback loop: on the next run, the bot uses the corrected answer directly
from config (Tier 1) instead of invoking the LLM again.

Implementation: `src/velvetoverride/main.py` → `review` command with `--approve`
flag, `_persist_learned_answers()`. Uses `field_solver.learn_answer()` +
`db.approve_answer()`.

### 12. Resume Fallback Strategy
When LLM-powered resume tailoring fails (API error, rate limit, content issue),
the bot falls back to the most recently generated resume in `data/resumes/`, or
a configured static resume path. This ensures applications are never skipped
solely because resume generation failed — a degraded resume is better than no
application.

Implementation: `src/velvetoverride/main.py` → `_find_default_resume()`, fallback
logic in the apply loop.

---

## How to Run

### Prerequisites

- **Python 3.11+**
- **Google Chrome** installed (the bot uses real Chrome, not Chromium)
- **OpenAI (ChatGPT) API key** (for LLM features — optional but recommended; Anthropic also supported)

### Quick Start

```bash
# 1. Clone and install
git clone <repo-url> && cd VelvetOverride
pip install -e ".[dev]"

# 2. Install Chrome browser for Patchright
patchright install chrome

# 3. Configure your credentials
cp config/.env.example config/.env
# Edit config/.env with your LinkedIn credentials and OPENAI_API_KEY

# 4. Customize your profile
# Personal data goes in gitignored *.local.yaml (never the committed templates):
cp config/profile.yaml config/profile.local.yaml   # then edit with your real data
cp config/answers.yaml config/answers.local.yaml    # your predetermined answers
# Edit config/settings.yaml to set job search filters

# 5. Run in dry-run mode first (fills forms but does NOT submit)
velvetoverride run --dry-run

# 6. When satisfied, run live
velvetoverride run --live
```

### CLI Commands

```bash
velvetoverride run [--dry-run|--live] [-v]   # Run the application bot
  --max-apps N                               # Max applications this run
  --min-salary N                             # Minimum annual salary (e.g. 100000)
  --max-salary N                             # Maximum annual salary (e.g. 200000)
velvetoverride token-test [-n N]             # Estimate token use on N sample jobs (no LinkedIn)
velvetoverride dashboard [--port P]          # Launch localhost tracking dashboard (default :5000)
velvetoverride stats                         # Enriched statistics (salary, sources, failures, runs, tokens, errors)
velvetoverride export [--format csv|json]    # Export tracking data (includes salary fields)
velvetoverride review [--approve]            # Show / interactively approve LLM-answered questions
velvetoverride runs [--limit N]              # Show recent bot run history
velvetoverride errors [--limit N]            # Show recorded errors from runs
velvetoverride tailor TITLE COMPANY JD_FILE  # Generate a tailored resume only
```

### Scheduled run (Windows, every 5 days at 9 AM PST)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_schedule.ps1  # register (every 5 days)
Start-ScheduledTask -TaskName "VelvetOverride"                          # test it now
```

The task computes the local-time equivalent of 9:00 AM Pacific, runs every
`-DaysInterval` days (default 5), and executes `scripts\run_daily.ps1`, which
respects `settings.yaml` and logs to `data/logs/`.

### Configuration Files

| File | Purpose |
|---|---|
| `config/settings.yaml` | Bot behavior, search filters, browser config, LLM provider/models, rate limits |
| `config/profile.yaml` | Your resume data — experience, skills, education, personal info |
| `config/answers.yaml` | Predetermined answers for known question types + learned answers |
| `config/.env` | Secrets — LinkedIn credentials, **OPENAI_API_KEY**, proxy URL |

### Where to Run

**Option A: Local machine (recommended for getting started)**
- Run on your desktop/laptop with Chrome installed
- Browser opens visually so you can watch and intervene
- Best for calibration and dry-run testing

**Option B: Cloud VM with display**
- Spin up a cloud VM (e.g., AWS EC2, GCP, DigitalOcean) with a desktop environment
- Use VNC/RDP to observe the bot
- Set `headless: false` in settings.yaml
- Good for longer unattended sessions

**Option C: Headless server (advanced)**
- Run on a headless Linux server with `Xvfb` (virtual framebuffer)
- `Xvfb :99 -screen 0 1920x1080x24 & export DISPLAY=:99`
- Set `headless: false` (Patchright still needs a display context for stealth)
- Best for scheduled/cron-based runs

### Running Tests

```bash
pytest tests/ -v              # Run all tests
pytest tests/ -v --cov        # Run with coverage
```

---

## Implementation Phases

All phases are **implemented**:

### Phase 1: Foundation (DONE)
- Project scaffolding (pyproject.toml, directory structure, config loaders)
- Patchright browser engine setup with anti-detection config
- LinkedIn authentication (login + session persistence + checkpoint handling)
- SQLite tracking database schema and models

### Phase 2: Job Discovery (DONE)
- Job search URL builder with full filter support
- Job listing scraper (title, company, description, URL, location)
- Deduplication (exact URL + fuzzy title matching)
- Company and keyword blacklisting

### Phase 3: Application Engine (DONE)
- Multi-step Easy Apply form walker
- Form field type detection (7 types: text, numeric, radio, dropdown, checkbox, upload, textarea)
- 5-tier hybrid field solver (learned → config → profile → EEO → LLM)
- Resume upload handling
- Per-step screenshot capture

### Phase 4: Resume Tailoring (DONE)
- Master resume YAML schema with skill-tagged bullet points
- Jinja2 HTML template with ATS-friendly layout
- LLM-powered JD keyword extraction and bullet ranking
- Professional summary rewriting per role
- ATS self-scoring with coverage analysis
- PDF generation via WeasyPrint

### Phase 5: Polish & Safety (DONE)
- Dry-run mode (forms filled but not submitted)
- Human review queue with flagging
- CSV/JSON/review-queue export
- Configurable rate limiting and human-like timing
- Activity diversification between applications
- Structured logging with structlog

### Phase 6: Salary Filtering & CAPTCHA Handling (DONE)
- Salary range extraction from job descriptions (regex, annual/hourly, multiple formats)
- Salary band filtering via CLI (`--min-salary`/`--max-salary`) and settings.yaml
- Salary data tracked in SQLite (salary_min, salary_max, salary_raw columns)
- CAPTCHA detection and 3-strategy resolution (manual, 2Captcha API, CapSolver API)
- CAPTCHA integration into login flow and post-login verification
- CLI `--max-apps` flag to control application count per run
- 57-test suite covering config, field solver, resume, salary, search, and tracking

### Phase 7: Observability, Resilience & Bug Fixes (DONE)

Comprehensive audit of the entire codebase identified 30 issues across 8 categories.
All critical and high-priority issues were fixed. See the **Design Strategy & Audit**
section below for the full analysis.

**Bug fixes:**
- Fuzzy dedup `continue` was in the inner loop scope, silently applying to duplicates
- Auth login `delay=random_delay` passed an async function object instead of integer ms
- LLM response extraction crashed on empty/unexpected response content blocks

**Data completeness:**
- Salary data (min, max, raw) now included in CSV and JSON exports
- Screenshot paths tracked per form step and linked to application records
- Job description truncation increased to 5,000 chars with logging when it occurs
- Full job descriptions exported in JSON (previously truncated to 500 chars)

**Run-level tracking:**
- New `runs` table tracks every bot execution (start/end time, config, counts, errors)
- Applications linked to their run via `run_id` foreign key
- `velvetoverride runs` CLI command shows run history
- Run status properly set on all exit paths (login fail, CAPTCHA fail, completion, crash)

**Enriched observability:**
- `velvetoverride stats` now shows: answer source breakdown, salary statistics,
  top failure reasons, and recent run history
- File-based logging support via `log_dir` parameter (alongside stderr)

**Answer memory persistence:**
- `velvetoverride review --approve` interactive workflow: approve or correct LLM answers
- Approved/corrected answers persisted to `config/answers.yaml` `learned` section
- On future runs, learned answers are used directly (zero API cost)

**Resilience:**
- Resume fallback: when LLM tailoring fails, falls back to most recent resume or
  configured static resume path
- FK enforcement (`PRAGMA foreign_keys = ON`) for database integrity
- Schema migrations for existing databases (safe `ALTER TABLE` with error handling)
- Fatal error handling: run status set to "failed" with error message before crash

**Test suite: 70 tests** covering config (3), field solver (18), resume (7),
salary (20), search (6), tracking + runs + approvals (22 — up from 6).

### Phase 8: OpenAI (ChatGPT), Robust Dedup, Dashboard & Scheduling (DONE)

Migrated the LLM layer to OpenAI, hardened de-duplication, added a localhost
tracking dashboard, and set up a small daily scheduled run.

**LLM provider migration (OpenAI / ChatGPT):**
- `llm.py` is now provider-agnostic (`llm.provider: openai | anthropic`). OpenAI is
  the default via `gpt-4.1-mini` (field Q&A) + `gpt-4.1` (resume tailoring).
- Config reads `OPENAI_API_KEY`; `config.has_llm` gates on the active provider.
- **Free token sharing:** models are chosen to fit OpenAI's complimentary daily
  token bucket (mini models ~2.5M/day, standard ~250K/day at tier 1-2). Prompt
  excerpts were trimmed and `field_max_tokens` lowered to keep usage tiny.
- Every LLM call records token usage (`Usage` accumulator) with per-model cost
  estimation; usage is persisted per run (`runs.prompt_tokens/…/est_cost_usd`).

**Token-use evaluation:**
- New `token-test` command + `agent/token_test.py` run the exact per-job LLM calls
  against sample JDs (no LinkedIn) and report tokens/job + daily/monthly projection.
- Measured: **~2,093 tokens/job** across 5 calls → at 5 apps/day ≈ 10.5K tokens/day
  (~0.4% of the free mini bucket), ~$0.01/day at billed rates.

**Robust de-duplication (never re-apply to the same job):**
- `utils/urls.py` extracts the canonical LinkedIn **Job ID** and normalizes every
  URL shape (`/jobs/view/<id>`, slug URLs, `currentJobId=`, relative, tracking params)
  to one canonical form.
- New `applications.job_id` column + index (with migration). `is_already_applied`
  matches by job ID first, then normalized URL; fuzzy title+company remains as a
  third layer. Scraper populates `job_id` and dedups across keyword searches.

**Targeting & recency:**
- Keywords are now searched **separately** (Data Scientist, Software Engineer, AI
  Engineer, Machine Learning Engineer) and merged/deduped by job ID.
- `date_posted: past_24h` + `sort_by: date` (`sortBy=DD`) → only fresh postings.
- `max_applications: 5` for a small daily cadence.

**Resume tailoring guardrails:**
- Bullet text is always used verbatim (never LLM-rewritten). By default all bullets
  are kept in original order (`reorder_bullets: false`, `max_bullets_per_job: null`),
  so real experience and impact metrics are never dropped or altered. The summary
  prompt forbids inventing skills/metrics.

**Error tracking:**
- New `errors` table + `log_error`/`get_errors`/`error_count`. Resume-tailor, apply,
  and fatal errors are recorded with stage/type/company/job context and surfaced via
  the `errors` CLI and the dashboard.

**Localhost dashboard (`web/dashboard.py`):**
- Flask app + JSON API (`/api/summary|applications|fit|config|runs|errors`) with a
  single-page dark UI: summary cards (applied, failed, skipped, errors, avg fit) and
  tabs for Applications, Experience fit, **Config & search** (the roles/locations
  searched + all effective settings, secret-redacted), Runs, and Errors.
  Auto-refreshes every 15s. `velvetoverride dashboard` launches it (default
  http://127.0.0.1:5000).

**Daily scheduling (Windows):**
- `scripts/register_schedule.ps1` registers a per-user Task Scheduler job at the
  local-time equivalent of 9:00 AM Pacific (DST-aware). `scripts/run_daily.ps1`
  runs one pipeline pass and logs to `data/logs/`.

**Profile data:** committed `config/profile.yaml` is a safe **placeholder**; real
data lives in gitignored `config/profile.local.yaml` (the loader prefers `*.local.yaml`).

**Test suite: 88 tests** (+18): adds `test_urls.py` (7 — job-ID/URL normalization) and
`test_dedup_and_tracking.py` (11 — job_id dedup, error logging, token accounting).

### Phase 9: Full-repo review, hardening, and configurability (DONE)

A multi-agent review (9 subsystems + docs, adversarially verified) surfaced 50
confirmed bugs. All HIGH/MEDIUM and most LOW were fixed, plus three features.

**Critical fixes:** never upload a non-PDF resume (build() now raises instead of
returning `.html`; xhtml2pdf is a core dep); Easy Apply **submit is verified**
before recording APPLIED; manual/SSO login no longer wipes itself (passive cookie
probe instead of re-navigating); DB migration indexes created after their columns
(no crash on upgraded DBs). **Reliability:** match/salary re-checked on the full JD;
failed jobs stay retryable through fuzzy dedup; multi-location searched separately;
external tab closed + scoped to the form; radio-group labels; word-boundary
years-matching (C++/C#); config-driven location preference (de-hardcoded Seattle);
consent-only checkbox ticking; adverse yes/no never auto-"Yes"; proxy credentials;
UTF-8 CSV/JSON export; dashboard XSS/quote escaping + path-traversal fix.

**Features:** (1) dashboard **Config & search** tab + `/api/config` showing the
roles/locations searched and all effective settings (secret-redacted); (2)
**beyond Easy Apply** promoted to a first-class `external_apply:` section
(enabled / submit / max_pages); (3) **bring-your-own-resume** via `resume.mode:
static` + `resume.static_resume_path`, and multi-user onboarding via the
`*.local.yaml` precedence + placeholder guard.

**Test suite: 172 tests.**

---

## Design Strategy & Audit

### Audit Methodology

A full-codebase audit was conducted to identify failure points, data loss risks,
and silent errors. The audit examined every file in the `src/` tree against these
categories:

1. **Data Loss** — Where does information get dropped or fail to persist?
2. **Silent Failures** — Where do errors get swallowed without visibility?
3. **Error Handling** — Where can crashes occur from unvalidated inputs?
4. **Database Issues** — Schema gaps, missing constraints, query safety?
5. **Logic Bugs** — Incorrect control flow, wrong variable scope, type errors?
6. **Configuration** — Edge cases in YAML loading, missing keys, type coercion?
7. **Logging Gaps** — Where do operations complete without any trace?

### Critical Bugs Found & Fixed

#### 1. Fuzzy Dedup Loop Scope Bug (`main.py`)
**Severity: Critical (Data Loss)**

The `continue` statement inside the fuzzy dedup check was in the inner `for s
in similar` loop, not the outer `for i, listing in enumerate(listings)` loop.
This meant that even when a fuzzy duplicate was detected, the bot would still
apply to the job — the `continue` only skipped one iteration of the similarity
check, not the application.

**Fix:** Introduced `is_fuzzy_dup` flag with `break` from inner loop, then
`if is_fuzzy_dup: continue` at the outer loop level.

```python
# Before (broken):
for s in similar:
    if fuzz.ratio(...) > 85:
        continue  # ← Only skips inner loop iteration!

# After (fixed):
is_fuzzy_dup = False
for s in similar:
    if fuzz.ratio(...) > 85:
        is_fuzzy_dup = True
        break
if is_fuzzy_dup:
    continue  # ← Correctly skips the outer application loop
```

#### 2. Auth Typing Delay Type Error (`auth.py`)
**Severity: Critical (Runtime Crash)**

`delay=random_delay` passed the async function object (from `stealth.py`) instead
of calling `random.randint(50, 150)` for a numeric millisecond value. Playwright's
`.type()` method expects an integer for the `delay` parameter. This would cause
either a type error or bizarre behavior depending on how Playwright handles a
coroutine object as a delay value.

**Fix:** Replaced with `delay=random.randint(50, 150)` using stdlib `random`.

#### 3. LLM Empty Response Crash (`llm.py`)
**Severity: High (Runtime Crash)**

All 5 LLM API call sites used `response.content[0].text.strip()` without checking
if `response.content` was empty or if the first block had a `text` attribute. An
empty response (API error, rate limit, content filter) would crash with `IndexError`.

**Fix:** Introduced `_extract_text()` static method that safely handles:
- Empty `response.content` → returns `""` with warning log
- Block without `text` attribute → returns `str(block)` with warning log
- Normal case → returns `block.text.strip()`

### Data Flow Design

```
                    ┌──────────────────────────────────────────────┐
                    │              Bot Run Lifecycle               │
                    │                                              │
                    │  db.start_run() ──→ run_id                  │
                    │       │                                      │
                    │       ▼                                      │
                    │  Login ──→ fail? ──→ finish_run("failed")   │
                    │       │                                      │
                    │       ▼                                      │
                    │  Search ──→ 0 listings? ──→ finish_run()    │
                    │       │                                      │
                    │       ▼                                      │
                    │  Score → Filter → For each:                  │
                    │       │                                      │
                    │       ├─ Dedup (URL + fuzzy)                 │
                    │       ├─ Tailor resume (fallback on fail)    │
                    │       ├─ Apply (fill forms, screenshot)      │
                    │       └─ save_application(record, run_id)    │
                    │       │                                      │
                    │       ▼                                      │
                    │  Export CSV/JSON ──→ finish_run("completed") │
                    │                                              │
                    │  On crash: finish_run("failed", error=...)   │
                    └──────────────────────────────────────────────┘
```

### Answer Memory Lifecycle

```
  Novel question ──→ LLM generates answer ──→ Saved with needs_review=True
                                                        │
                                                        ▼
                                          velvetoverride review --approve
                                                        │
                                            ┌───────────┼───────────┐
                                            ▼           ▼           ▼
                                         [a]pprove  [c]orrect   [s]kip
                                            │           │
                                            ▼           ▼
                                   db.approve_answer()  db.approve_answer()
                                   learn_answer(q, a)   learn_answer(q, new_a)
                                            │           │
                                            └─────┬─────┘
                                                  ▼
                                    _persist_learned_answers()
                                    → writes to answers.yaml
                                                  │
                                                  ▼
                                    Next run: Tier 1 lookup hits
                                    → zero API cost, zero latency
```

### Observability Stack

| Layer | What | Where |
|---|---|---|
| **stderr** | Real-time structured logs | Console (structlog ConsoleRenderer) |
| **File logs** | Persistent JSON logs per run | `data/logs/velvetoverride_YYYYMMDD_HHMMSS.log` |
| **Screenshots** | Visual audit trail per form step | `screenshots/<company>_<timestamp>_stepN.png` |
| **SQLite: runs** | Run-level metadata | `data/applications.db` → `runs` table |
| **SQLite: applications** | Per-job outcome + salary | `data/applications.db` → `applications` table |
| **SQLite: questions** | Per-field Q&A + source + review flag | `data/applications.db` → `questions` table |
| **CSV export** | Spreadsheet-friendly summary | `data/applications.csv` |
| **JSON export** | Full data including JDs and questions | `data/applications.json` |
| **Review queue** | Questions needing human review | `data/review_queue.json` |
| **CLI: stats** | Enriched dashboard (sources, salary, failures, runs) | `velvetoverride stats` |
| **CLI: runs** | Run history with per-run counts | `velvetoverride runs` |
| **CLI: review** | Interactive approval workflow | `velvetoverride review --approve` |

### Database Schema (v2)

```sql
-- Run-level tracking (new)
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT NULL,
    status TEXT DEFAULT 'running',       -- running, completed, failed
    dry_run INTEGER DEFAULT 1,
    max_apps INTEGER DEFAULT 25,
    min_salary INTEGER DEFAULT NULL,
    max_salary INTEGER DEFAULT NULL,
    listings_found INTEGER DEFAULT 0,
    listings_after_filter INTEGER DEFAULT 0,
    applied_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    error_message TEXT DEFAULT '',
    config_snapshot TEXT DEFAULT ''       -- JSON snapshot of search config
);

-- Application tracking (enhanced)
CREATE TABLE applications (
    -- ... existing columns ...
    salary_min INTEGER DEFAULT NULL,     -- new: extracted salary floor
    salary_max INTEGER DEFAULT NULL,     -- new: extracted salary ceiling
    salary_raw TEXT DEFAULT '',           -- new: raw salary text from JD
    run_id INTEGER DEFAULT NULL,         -- new: links to runs table
    screenshot_path TEXT DEFAULT ''       -- now populated with semicolon-joined paths
);

-- FK enforcement enabled
PRAGMA foreign_keys = ON;

-- Schema migrations handle existing databases safely
ALTER TABLE applications ADD COLUMN salary_min INTEGER DEFAULT NULL;
-- (silently ignored if column already exists)
```

---

## Legal & Ethical Disclaimer

**This project is for educational and personal use only.** Automated interaction with
LinkedIn may violate their [Terms of Service](https://www.linkedin.com/help/linkedin/answer/a1341387).
Users assume all risk of account suspension or legal action. The authors do not encourage
or endorse any use that violates platform terms of service.

Recommendations:
- Always operate on your own LinkedIn account
- Use conservative rate limits (20-30 applications per session max)
- Review all LLM-generated answers before submission (dry-run first)
- Do not scrape or store other users' personal data
- Comply with GDPR and applicable data protection laws
- Consider using LinkedIn's official API where it meets your needs

---

## References

- [Auto_job_applier_linkedIn](https://github.com/GodsScion/Auto_job_applier_linkedIn) — Most feature-complete existing bot
- [LinkedIn-Easy-Apply-Bot](https://github.com/NathanDuma/LinkedIn-Easy-Apply-Bot) — Best YAML config-driven Q&A approach
- [browser-use](https://github.com/browser-use/browser-use) — LLM agent browser automation framework
- [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) — Undetected Playwright fork
- [Skyvern](https://github.com/Skyvern-AI/skyvern) — LLM + computer vision browser automation
- [Anthropic Computer Use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) — Claude vision-based automation API
- [LinkedIn Easy Apply API](https://learn.microsoft.com/en-us/linkedin/talent/easy-apply/easy-apply) — Official form field documentation
- [ScrapeOps LinkedIn Guide](https://scrapeops.io/websites/linkedin/) — Anti-detection deep dive
- [Scrapfly LinkedIn Scraping](https://scrapfly.io/blog/posts/how-to-scrape-linkedin) — Scraping methodology
- [BrightData Playwright Stealth](https://brightdata.com/blog/how-tos/avoid-bot-detection-with-playwright-stealth) — Stealth techniques
- [Reztune ATS Optimization](https://www.reztune.com/blog/best-ai-resume-tailoring-2025/) — Resume keyword optimization
- [Jobscan](https://www.jobscan.co/) — ATS scoring methodology
- [hanula/resume](https://github.com/hanula/resume) — YAML → PDF resume generator
- [pyresume](https://pypi.org/project/pyresume/) — Templated LaTeX resume generation
- [Firecrawl Browser Agents Guide](https://www.firecrawl.dev/blog/best-browser-agents) — 2026 agent landscape comparison
