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
│       │   ├── llm.py           # Claude API client (Q&A, keyword extraction, tailoring)
│       │   ├── field_solver.py  # 5-tier hybrid solver (learned→config→profile→EEO→LLM)
│       │   ├── resume_tailor.py # JD analysis → bullet ranking → summary rewrite → PDF
│       │   └── salary.py        # Salary range extraction (regex) + filtering
│       ├── resume/
│       │   ├── builder.py       # Jinja2 → HTML → PDF (WeasyPrint) pipeline
│       │   ├── scorer.py        # ATS keyword coverage scoring & suggestions
│       │   └── templates/
│       │       └── default.html.j2  # Professional ATS-friendly resume template
│       ├── tracking/
│       │   ├── database.py      # SQLite schema, CRUD, dedup, stats, review queue
│       │   ├── models.py        # ApplicationRecord, QuestionRecord, JobListing, enums
│       │   └── export.py        # CSV/JSON/review-queue export
│       └── utils/
│           ├── config.py        # YAML config loader, .env secrets, Config dataclass
│           └── logging.py       # structlog setup (console + JSON modes)
├── tests/
│   ├── test_config.py           # Config loading (3 tests)
│   ├── test_field_solver.py     # Hybrid field solver (18 tests)
│   ├── test_resume.py           # Resume builder + ATS scorer (7 tests)
│   ├── test_salary.py           # Salary extraction + filtering (20 tests)
│   ├── test_search.py           # Search URL builder (6 tests)
│   └── test_tracking.py         # SQLite database (6 tests)
└── data/                        # (gitignored)
    ├── applications.db          # SQLite tracking database
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
| LLM Provider | **Anthropic Claude API** (Sonnet for speed, Opus for complex reasoning) | Strong instruction-following, tool use, long context for JD analysis |
| Resume Templates | **Jinja2 + LaTeX** (`pdflatex`) or **Jinja2 + HTML** (`weasyprint`) | Separation of data/layout, ATS-friendly PDF output |
| Resume Data | **YAML** | Human-readable, easy to maintain |
| Database | **SQLite** (via `sqlite3` stdlib) | Zero-config, portable, sufficient for local tracking |
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

3. **Claude API over OpenAI:** Stronger instruction-following for form field reasoning,
   native tool use support for structured output, and extended thinking for complex JD
   analysis. Sonnet for fast field Q&A, Opus for resume tailoring and complex reasoning.

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

---

## How to Run

### Prerequisites

- **Python 3.11+**
- **Google Chrome** installed (the bot uses real Chrome, not Chromium)
- **Anthropic API key** (for LLM features — optional but recommended)

### Quick Start

```bash
# 1. Clone and install
git clone <repo-url> && cd VelvetOverride
pip install -e ".[dev]"

# 2. Install Chrome browser for Patchright
patchright install chrome

# 3. Configure your credentials
cp config/.env.example config/.env
# Edit config/.env with your LinkedIn credentials and Anthropic API key

# 4. Customize your profile
# Edit config/profile.yaml with your real experience, skills, education
# Edit config/answers.yaml to set your predetermined answers
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
velvetoverride stats                         # Show application statistics
velvetoverride export [--format csv|json]    # Export tracking data
velvetoverride review                        # Show questions needing review
velvetoverride tailor TITLE COMPANY JD_FILE  # Generate a tailored resume only
```

### Configuration Files

| File | Purpose |
|---|---|
| `config/settings.yaml` | Bot behavior, search filters, browser config, LLM models, rate limits |
| `config/profile.yaml` | Your resume data — experience, skills, education, personal info |
| `config/answers.yaml` | Predetermined answers for known question types + learned answers |
| `config/.env` | Secrets — LinkedIn credentials, Anthropic API key, proxy URL |

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
