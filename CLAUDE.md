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
├── CLAUDE.md                    # This file
├── pyproject.toml               # Project config (uv/pip)
├── config/
│   ├── settings.yaml            # Bot settings (rate limits, filters, browser config)
│   ├── profile.yaml             # User profile data (master resume, personal info)
│   ├── answers.yaml             # Predetermined answers for known question types
│   └── .env                     # Secrets (LinkedIn creds, API keys) — GITIGNORED
├── src/
│   └── velvetoverride/
│       ├── __init__.py
│       ├── main.py              # CLI entry point / orchestrator
│       ├── browser/
│       │   ├── __init__.py
│       │   ├── engine.py        # Patchright browser setup & lifecycle
│       │   ├── stealth.py       # Anti-detection config & human-like behavior
│       │   └── pages.py         # Page object models for LinkedIn pages
│       ├── linkedin/
│       │   ├── __init__.py
│       │   ├── auth.py          # Login & session management
│       │   ├── search.py        # Job search & listing scraper
│       │   ├── apply.py         # Application flow controller
│       │   └── fields.py        # Form field detection & interaction
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── llm.py           # Claude API client (form Q&A, resume tailoring)
│       │   ├── field_solver.py  # Hybrid: config lookup → LLM fallback for fields
│       │   └── resume_tailor.py # JD analysis → keyword extraction → resume gen
│       ├── resume/
│       │   ├── __init__.py
│       │   ├── builder.py       # YAML → Jinja2 → PDF pipeline
│       │   ├── scorer.py        # ATS keyword match scoring
│       │   └── templates/       # Jinja2 LaTeX/HTML resume templates
│       │       └── default.tex.j2
│       ├── tracking/
│       │   ├── __init__.py
│       │   ├── database.py      # SQLite schema & CRUD operations
│       │   ├── models.py        # Application, Company, Question data models
│       │   └── export.py        # CSV/JSON export for human review
│       └── utils/
│           ├── __init__.py
│           ├── config.py        # YAML config loader
│           └── logging.py       # Structured logging
├── tests/
│   ├── test_fields.py           # Form field detection tests
│   ├── test_resume.py           # Resume generation tests
│   ├── test_tracking.py         # Database tests
│   └── test_agent.py            # LLM integration tests
└── data/
    ├── applications.db          # SQLite database (gitignored)
    └── resumes/                 # Generated resume PDFs (gitignored)
```

### Core Flow

```
1. CONFIGURE → Load profile.yaml, answers.yaml, settings.yaml
2. LAUNCH    → Start Patchright persistent browser context (real Chrome)
3. LOGIN     → Authenticate to LinkedIn (session cookie reuse when possible)
4. SEARCH    → Navigate to Jobs, apply filters (role, location, Easy Apply, etc.)
5. ITERATE   → For each job listing:
   a. SCRAPE   → Extract job title, company, description, requirements
   b. CHECK    → Deduplicate against tracking DB
   c. TAILOR   → Generate role-specific resume via LLM + Jinja2 pipeline
   d. APPLY    → Click Easy Apply, walk through multi-step form:
      i.   Detect field type (text, radio, dropdown, checkbox, upload, textarea)
      ii.  Lookup answer in answers.yaml config
      iii. If no config match → invoke Claude API for contextual answer
      iv.  Upload tailored resume PDF
      v.   Flag LLM-generated answers for human review
   e. TRACK    → Record application details in SQLite
   f. DELAY    → Human-like pause before next application
6. EXPORT   → Generate CSV/JSON report of all applications for review
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

## Implementation Phases

### Phase 1: Foundation
- Project scaffolding (pyproject.toml, directory structure, config loaders)
- Patchright browser engine setup with anti-detection config
- LinkedIn authentication (login + session persistence)
- SQLite tracking database schema and models

### Phase 2: Job Discovery
- Job search page navigation and filter application
- Job listing scraper (title, company, description, URL, requirements)
- Deduplication against tracking database
- Job description storage and keyword extraction

### Phase 3: Application Engine
- Easy Apply form flow walker (multi-step form navigation)
- Form field type detection (text, radio, dropdown, checkbox, upload, textarea)
- Predetermined answer lookup from `answers.yaml`
- Claude API integration for unknown field resolution
- Resume upload handling

### Phase 4: Resume Tailoring
- Master resume YAML schema design
- Jinja2 LaTeX/HTML template creation
- LLM-powered JD keyword extraction and bullet point ranking
- Professional summary rewriting per role
- ATS score self-evaluation
- PDF generation pipeline

### Phase 5: Polish & Safety
- Dry-run mode (fill forms without submitting)
- Human review queue and flagging system
- CSV/JSON export for application tracking
- Rate limiting and human-like timing tuning
- Comprehensive logging and error recovery
- Test suite for core components

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
