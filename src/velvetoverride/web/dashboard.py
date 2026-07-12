"""Flask dashboard — a localhost site to track application progress.

Serves a single-page dashboard plus a small JSON API, all backed by the same
SQLite tracking DB the bot writes to. Read-only; safe to leave running.

Run via:  velvetoverride dashboard  (defaults to http://127.0.0.1:5000)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template_string, send_file

from velvetoverride.tracking.database import TrackingDB


def _db(db_path: str) -> TrackingDB:
    db = TrackingDB(db_path)
    db.connect()
    return db


def _effective_config() -> dict:
    """Load the EFFECTIVE (local-first) config and return a redacted, UI-safe
    view — the roles/locations being searched and all behavioral settings.

    Never includes secrets (API keys, passwords).
    """
    try:
        from velvetoverride.utils.config import load_config
        c = load_config()
    except Exception as e:  # config not found / invalid
        return {"error": str(e)}

    search = c.search
    bot = c.bot
    ext = c.settings.get("external_apply", {}) or {}
    return {
        "profile": {
            "name": f"{c.personal.get('first_name','')} {c.personal.get('last_name','')}".strip(),
            "email": c.personal.get("email", ""),
            "location": c.personal.get("location") or c.personal.get("city", ""),
            "is_placeholder": str(c.personal.get("email", "")).lower()
                in ("jane.doe@example.com", "you@example.com"),
        },
        "search": {
            "keywords": search.get("keywords", []),
            "locations": search.get("locations", []),
            "date_posted": search.get("date_posted", ""),
            "remote": search.get("remote", []),
            "experience_levels": search.get("experience_levels", []),
            "job_types": search.get("job_types", []),
            "easy_apply_only": search.get("easy_apply_only", True),
            "max_pages": search.get("max_pages", 3),
            "min_match_score": search.get("min_match_score", 0),
            "blacklist_companies": search.get("blacklist_companies", []),
            "blacklist_keywords": search.get("blacklist_keywords", []),
        },
        "bot": {
            "dry_run": bot.get("dry_run", True),
            "max_applications": bot.get("max_applications", 25),
        },
        "external_apply": {
            "enabled": ext.get("enabled", bot.get("external_apply", True)),
            "submit": ext.get("submit", False),
            "max_pages": ext.get("max_pages", bot.get("external_max_pages", 8)),
        },
        "resume": {
            "mode": c.resume_config.get("mode", "tailored"),
            "static_resume_path": c.resume_config.get("static_resume_path", ""),
        },
        "salary": {
            "min_annual": c.settings.get("salary", {}).get("min_annual"),
            "max_annual": c.settings.get("salary", {}).get("max_annual"),
        },
        "fit": c.settings.get("fit", {}),
        "llm": {
            "provider": c.llm_provider,
            "field_model": c.llm.get("field_model", ""),
            "resume_model": c.llm.get("resume_model", ""),
            "configured": c.has_llm,
        },
    }


def create_app(db_path: str = "data/applications.db", resume_dir: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    # Resumes live next to the DB (both under data/) unless told otherwise.
    if resume_dir is None:
        resume_dir = Path(db_path).resolve().parent / "resumes"
    app.config["RESUME_DIR"] = Path(resume_dir).resolve()

    @app.route("/")
    def index() -> str:
        return render_template_string(_PAGE)

    @app.route("/api/summary")
    def api_summary() -> Any:
        db = _db(app.config["DB_PATH"])
        try:
            stats = db.get_stats()
            stats["errors_total"] = db.error_count()
            stats["fit"] = db.get_fit_summary()
            return jsonify(stats)
        finally:
            db.close()

    @app.route("/api/applications")
    def api_applications() -> Any:
        db = _db(app.config["DB_PATH"])
        try:
            apps = db.get_applications(limit=500)
            out = []
            for a in apps:
                out.append({
                    "id": a.id,
                    "company": a.company,
                    "job_title": a.job_title,
                    "job_id": a.job_id,
                    "job_url": a.job_url,
                    "location": a.location,
                    "status": a.status,
                    "match_score": round(a.match_score or 0, 1),
                    "resume": Path(a.resume_version).name if a.resume_version else "",
                    "fit_score": a.fit_score,
                    "fit_seniority": a.fit_seniority or "",
                    "fit_recommend": a.fit_recommend or "",
                    "fit_reasoning": a.fit_reasoning or "",
                    "notes": a.notes or "",
                    "applied_at": a.applied_at,
                    "salary_min": a.salary_min,
                    "salary_max": a.salary_max,
                    "questions": len(a.questions),
                    "needs_review": sum(1 for q in a.questions if q.needs_review),
                })
            return jsonify(out)
        finally:
            db.close()

    @app.route("/api/runs")
    def api_runs() -> Any:
        db = _db(app.config["DB_PATH"])
        try:
            return jsonify(db.get_runs(limit=50))
        finally:
            db.close()

    @app.route("/api/fit")
    def api_fit() -> Any:
        db = _db(app.config["DB_PATH"])
        try:
            return jsonify(db.get_fit_rows(limit=300))
        finally:
            db.close()

    @app.route("/api/config")
    def api_config() -> Any:
        # The effective (local-first) search + behavior config, redacted.
        return jsonify(_effective_config())

    @app.route("/api/feedback")
    def api_feedback() -> Any:
        # Fields flagged for review, each with the routing problem (if any), so
        # you can see which field got which answer and improve routing.
        from velvetoverride.tracking.audit import flag_routing_problem
        db = _db(app.config["DB_PATH"])
        try:
            rows = db.get_needs_review()
            out = []
            for r in rows:
                problem = flag_routing_problem(
                    r.get("question_text", ""), r.get("answer_given", ""),
                    r.get("field_type", ""), r.get("answer_source", ""),
                )
                out.append({
                    "company": r.get("company", ""),
                    "job_title": r.get("job_title", ""),
                    "field": r.get("question_text", ""),
                    "field_type": r.get("field_type", ""),
                    "answer": r.get("answer_given", ""),
                    "source": r.get("answer_source", ""),
                    "problem": problem,
                })
            # Flagged-with-problem first
            out.sort(key=lambda x: (x["problem"] is None, x["company"]))
            return jsonify(out)
        finally:
            db.close()

    @app.route("/resume/<path:name>")
    def resume_file(name: str):
        # Serve a generated resume PDF for review. Path-traversal safe: resolve
        # and require the target be INSIDE the resume dir (is_relative_to, not a
        # bare startswith which would allow a sibling like "data/resumes_evil").
        resume_root = app.config["RESUME_DIR"]
        target = (resume_root / name).resolve()
        try:
            inside = target.is_relative_to(resume_root)
        except AttributeError:  # Python < 3.9
            import os
            inside = os.path.commonpath([str(target), str(resume_root)]) == str(resume_root)
        if not inside or not target.exists() or not target.is_file():
            abort(404)
        return send_file(str(target))

    @app.route("/api/errors")
    def api_errors() -> Any:
        db = _db(app.config["DB_PATH"])
        try:
            return jsonify(db.get_errors(limit=200))
        finally:
            db.close()

    return app


def run_dashboard(db_path: str = "data/applications.db", host: str = "127.0.0.1", port: int = 5000) -> None:
    # Ensure the DB/schema exists so the first page load doesn't error
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _db(db_path).close()
    app = create_app(db_path)
    app.run(host=host, port=port, debug=False)


_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VelvetOverride — Application Tracker</title>
<style>
  :root {
    --bg: #0f1115; --panel: #1a1d24; --panel2: #232733; --text: #e6e8ec;
    --muted: #98a0b3; --accent: #7c5cff; --accent2: #22c55e; --danger: #ef4444;
    --border: #2b303b;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); }
  header { padding: 20px 28px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; }
  header h1 { margin: 0; font-size: 18px; letter-spacing: .5px; }
  header h1 .v { color: var(--accent); }
  .muted { color: var(--muted); }
  .wrap { padding: 24px 28px; max-width: 1200px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 24px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .card .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
  .card .value { font-size: 26px; font-weight: 700; margin-top: 6px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
  .tab { padding: 8px 16px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; cursor: pointer; font-size: 14px; }
  .tab.active { background: var(--accent); border-color: var(--accent); }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border-radius: 12px; overflow: hidden; }
  th, td { text-align: left; padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); text-transform: uppercase; font-size: 11px; letter-spacing: .5px; }
  tr:hover td { background: var(--panel2); }
  a { color: var(--accent); text-decoration: none; }
  .pill { padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; display: inline-block; }
  .s-applied { background: #14331f; color: #4ade80; }
  .s-dry_run { background: #2a2140; color: #c4b5fd; }
  .s-needs_review { background: #3a2e12; color: #fbbf24; }
  .s-failed { background: #3a1717; color: #f87171; }
  .s-skipped { background: #23272f; color: #98a0b3; }
  .fit-good { color: #4ade80; font-weight: 700; }
  .fit-mid  { color: #fbbf24; font-weight: 700; }
  .fit-bad  { color: #f87171; font-weight: 700; }
  .fit-legend { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; margin-bottom: 12px; font-size: 13px; }
  .bar-wrap { display: inline-block; width: 70px; height: 6px; background: var(--panel2);
    border-radius: 3px; overflow: hidden; vertical-align: middle; }
  .bar { height: 100%; border-radius: 3px; }
  .bar-num { font-weight: 700; margin-left: 7px; font-size: 12px; }
  .score-cell { white-space: nowrap; }
  .gaps { color: #cbd5e1; font-size: 12px; max-width: 260px; }
  .reason { color: var(--muted); font-size: 12px; max-width: 300px; }
  .field-cell { color: #cbd5e1; font-size: 12.5px; max-width: 260px; }
  .prob-cell { max-width: 240px; }
  .flagged-row td { background: rgba(255,107,107,0.07); }
  .prob-flag { display: inline-block; background: rgba(255,107,107,0.16); color: #ff8b8b;
    border: 1px solid rgba(255,107,107,0.35); border-radius: 6px; padding: 2px 7px; font-size: 12px; }
  .src-pill { display: inline-block; background: var(--panel2); border: 1px solid var(--border);
    border-radius: 6px; padding: 1px 7px; font-size: 11.5px; color: var(--muted); }
  .hidden { display: none; }
  .refresh { font-size: 12px; color: var(--muted); cursor: pointer; }
  .empty { padding: 30px; text-align: center; color: var(--muted); }
  .cfg-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
  .cfg-sec { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
  .cfg-h { font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: var(--accent); margin-bottom: 8px; font-weight: 700; }
  .cfg-t { background: transparent; }
  .cfg-t td { border-bottom: 1px solid var(--border); padding: 6px 4px; vertical-align: top; }
  .cfg-k { color: var(--muted); width: 45%; font-size: 12.5px; }
  .cfg-v { font-size: 12.5px; }
  .chip { display: inline-block; background: var(--panel2); border: 1px solid var(--border);
    border-radius: 999px; padding: 2px 9px; font-size: 11.5px; margin: 1px 0; }
  code { background: var(--panel2); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1><span class="v">Velvet</span>Override <span class="muted" style="font-size:13px;">application tracker</span></h1>
  <span class="refresh" onclick="loadAll()">↻ refresh · <span id="ts"></span></span>
</header>
<div class="wrap">
  <div id="errbanner" style="display:none;background:#3a1717;color:#f87171;border:1px solid #5b2323;border-radius:10px;padding:12px 16px;margin-bottom:16px;"></div>
  <div class="cards" id="cards"></div>
  <div class="tabs">
    <div class="tab active" data-tab="apps" onclick="showTab('apps')">Applications</div>
    <div class="tab" data-tab="fit" onclick="showTab('fit')">Experience fit</div>
    <div class="tab" data-tab="feedback" onclick="showTab('feedback')">Field feedback</div>
    <div class="tab" data-tab="config" onclick="showTab('config')">Config &amp; search</div>
    <div class="tab" data-tab="runs" onclick="showTab('runs')">Runs</div>
    <div class="tab" data-tab="errors" onclick="showTab('errors')">Errors</div>
  </div>
  <div id="apps"></div>
  <div id="fit" class="hidden"></div>
  <div id="feedback" class="hidden"></div>
  <div id="config" class="hidden"></div>
  <div id="runs" class="hidden"></div>
  <div id="errors" class="hidden"></div>
</div>
<script>
// Escape for both text AND double-quoted attribute contexts (quotes included).
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function money(n){ return n==null? '—' : '$'+Number(n).toLocaleString(); }
function num(n){ return (n==null?0:n).toLocaleString(); }

async function getJSON(url){
  try { const r = await fetch(url); if(!r.ok) return null; return await r.json(); }
  catch(e){ return null; }
}

async function loadAll(){
  const [sum, apps, fit, feedback, runs, errors, cfg] = await Promise.all([
    getJSON('/api/summary'), getJSON('/api/applications'), getJSON('/api/fit'),
    getJSON('/api/feedback'), getJSON('/api/runs'), getJSON('/api/errors'), getJSON('/api/config'),
  ]);
  const banner = document.getElementById('errbanner');
  if (sum == null && apps == null){
    banner.style.display = 'block';
    banner.textContent = 'Cannot reach the tracker API. Is the dashboard server running?';
    return;
  }
  banner.style.display = 'none';
  renderCards(sum || {});
  renderApps(apps || []);
  renderFit(fit || [], (sum && sum.fit) || {});
  renderFeedback(feedback || []);
  renderConfig(cfg || {});
  renderRuns(runs || []);
  renderErrors(errors || []);
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
}

function renderCards(s){
  const by = s.by_status || {};
  // needs_review apps ARE applied (submitted + flagged for answer review)
  const applied = (by.applied||0)+(by.dry_run||0)+(by.needs_review||0);
  const f = s.fit || {};
  const rec = f.by_recommend || {};
  const cards = [
    ['Total jobs processed', num(s.total_applications)],
    ['Applied / submitted', num(applied)],
    ['Failed', num(by.failed||0)],
    ['Skipped (dupes)', num(by.skipped||0)],
    ['Errors logged', num(s.errors_total)],
    ['Avg experience fit', f.avg_score != null ? f.avg_score : '—'],
    ['Strong fits', num(rec.apply||0)],
    ['Poor fits', num(rec.skip||0)],
  ];
  document.getElementById('cards').innerHTML = cards.map(c=>
    `<div class="card"><div class="label">${c[0]}</div><div class="value">${c[1]}</div></div>`).join('');
}

// needs_review means the application WAS submitted (its answers are just
// flagged), so display it as "applied".
// ChatGPT's honest experience-fit verdict; hover for its reasoning.
function fitCell(r){
  if (r.fit_score == null) return '<span class="muted">—</span>';
  const rec = r.fit_recommend || '';
  const cls = rec === 'apply' ? 'fit-good' : (rec === 'skip' ? 'fit-bad' : 'fit-mid');
  const sen = (r.fit_seniority || '').replace('_',' ');
  const tip = `${rec}${sen ? ' · ' + sen : ''}${r.fit_reasoning ? ' — ' + r.fit_reasoning : ''}`;
  return `<span class="${cls}" title="${esc(tip)}">${r.fit_score}</span>`
       + `<span class="muted" style="font-size:10px;"> ${esc(sen.split(' ')[0]||'')}</span>`;
}

function statusPill(s){
  const label = (s === 'needs_review') ? 'applied' : s;
  const cls = (s === 'needs_review') ? 'applied' : s;
  return `<span class="pill s-${esc(cls)}">${esc(label)}</span>`;
}

function renderApps(rows){
  if(!rows.length){ document.getElementById('apps').innerHTML='<div class="empty">No applications yet. Run the bot to populate this.</div>'; return; }
  const body = rows.map(r=>`<tr>
    <td>${esc(r.company)}</td>
    <td>${r.job_url?`<a href="${esc(r.job_url)}" target="_blank">${esc(r.job_title)}</a>`:esc(r.job_title)}</td>
    <td>${statusPill(r.status)}</td>
    <td>${fitCell(r)}</td>
    <td>${r.match_score}</td>
    <td>${r.salary_min?money(r.salary_min)+'–'+money(r.salary_max):'—'}</td>
    <td>${r.questions} ${r.needs_review?`<span class="muted">(${r.needs_review}⚑)</span>`:''}</td>
    <td class="muted" title="${esc(r.resume)}">${r.resume?`<a href="/resume/${encodeURIComponent(r.resume)}" target="_blank">${esc(r.resume.slice(0,26))}</a>`:'—'}</td>
    <td class="muted" title="${esc(r.notes)}">${esc((r.notes||'').slice(0,60))||'—'}</td>
    <td class="muted">${esc((r.applied_at||'').slice(0,16).replace('T',' '))}</td>
  </tr>`).join('');
  document.getElementById('apps').innerHTML =
    `<table><thead><tr><th>Company</th><th>Role</th><th>Status</th><th>Fit</th><th>Match</th><th>Salary</th><th>Q's</th><th>Resume</th><th>Outcome / reason</th><th>Applied</th></tr></thead><tbody>${body}</tbody></table>`;
}

function recPill(rec){
  const cls = rec === 'apply' ? 's-applied' : (rec === 'skip' ? 's-failed' : 's-needs_review');
  return `<span class="pill ${cls}">${esc(rec||'—')}</span>`;
}

function scoreBar(score){
  const s = Math.max(0, Math.min(100, score||0));
  const color = s >= 70 ? '#4ade80' : (s >= 45 ? '#fbbf24' : '#f87171');
  return `<div class="bar-wrap"><div class="bar" style="width:${s}%;background:${color}"></div></div>
          <span class="bar-num" style="color:${color}">${s}</span>`;
}

function renderFit(rows, summary){
  const el = document.getElementById('fit');
  if(!rows.length){
    el.innerHTML = '<div class="empty">No fit evaluations yet. ChatGPT scores each job against your real titles, years, and skills before applying.</div>';
    return;
  }
  const sen = summary.by_seniority || {};
  const legend = `<div class="fit-legend">
      <b>${rows.length}</b> jobs evaluated &middot; avg <b>${summary.avg_score ?? '—'}</b> &middot;
      under-qualified <b>${sen.under_qualified||0}</b> &middot;
      match <b>${sen.match||0}</b> &middot;
      over-qualified <b>${sen.over_qualified||0}</b>
      <div class="muted" style="margin-top:4px;">ChatGPT judges seniority from your actual titles and dates &mdash; not the title on the job post.</div>
    </div>`;

  const body = rows.map(r=>`<tr>
    <td>${esc(r.company)}</td>
    <td>${r.job_url?`<a href="${esc(r.job_url)}" target="_blank">${esc(r.job_title)}</a>`:esc(r.job_title)}</td>
    <td class="score-cell">${scoreBar(r.fit_score)}</td>
    <td>${esc((r.fit_seniority||'').replace('_',' '))||'—'}</td>
    <td>${recPill(r.fit_recommend)}</td>
    <td class="gaps">${r.fit_gaps? esc(r.fit_gaps) : '<span class="muted">none</span>'}</td>
    <td class="reason">${esc(r.fit_reasoning||'')}</td>
    <td>${statusPill(r.status)}</td>
  </tr>`).join('');

  el.innerHTML = legend +
    `<table><thead><tr><th>Company</th><th>Role</th><th>Fit score</th><th>Seniority</th>
     <th>Verdict</th><th>Gaps</th><th>Why</th><th>Outcome</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderFeedback(rows){
  const el = document.getElementById('feedback');
  if(!rows.length){
    el.innerHTML = '<div class="empty">No fields flagged yet. After each run, mis-routed fields (a name field that got a sentence, a bad zip, an LLM answer needing review) show up here so you can fix routing.</div>';
    return;
  }
  const flagged = rows.filter(r=>r.problem).length;
  const legend = `<div class="fit-legend">
      <b>${rows.length}</b> fields to review &middot;
      <b style="color:#ff6b6b;">${flagged}</b> with a routing problem &middot;
      ${rows.length-flagged} LLM answers for sign-off
      <div class="muted" style="margin-top:4px;">Problem rows are likely mis-routed &mdash; use them to tighten <code>answers.yaml</code> or the field solver.</div>
    </div>`;
  const body = rows.map(r=>`<tr class="${r.problem?'flagged-row':''}">
    <td>${esc(r.company)}</td>
    <td>${esc(r.job_title)}</td>
    <td class="field-cell">${esc(r.field)}</td>
    <td>${esc(r.answer)}</td>
    <td><span class="src-pill">${esc(r.source||'—')}</span></td>
    <td>${esc(r.field_type||'')}</td>
    <td class="prob-cell">${r.problem?`<span class="prob-flag">${esc(r.problem)}</span>`:'<span class="muted">review</span>'}</td>
  </tr>`).join('');
  el.innerHTML = legend +
    `<table><thead><tr><th>Company</th><th>Role</th><th>Field</th><th>Answer given</th>
     <th>Source</th><th>Type</th><th>Problem</th></tr></thead><tbody>${body}</tbody></table>`;
}

function chips(arr){
  if(!arr || !arr.length) return '<span class="muted">—</span>';
  return arr.map(x=>`<span class="chip">${esc(x)}</span>`).join(' ');
}
function yesno(b){ return b ? '<span class="fit-good">yes</span>' : '<span class="muted">no</span>'; }

function renderConfig(c){
  const el = document.getElementById('config');
  if(!c || c.error){
    el.innerHTML = `<div class="empty">Config unavailable${c&&c.error?': '+esc(c.error):''}</div>`;
    return;
  }
  const s = c.search||{}, p = c.profile||{}, ext=c.external_apply||{}, r=c.resume||{}, llm=c.llm||{}, fit=c.fit||{}, sal=c.salary||{}, bot=c.bot||{};
  const warn = p.is_placeholder
    ? '<div style="background:#3a2e12;color:#fbbf24;border:1px solid #5b4a1a;border-radius:8px;padding:8px 12px;margin-bottom:12px;">⚠ Profile is still the placeholder — copy config/profile.yaml → config/profile.local.yaml and edit it before applying.</div>'
    : '';
  const section = (title, rows) =>
    `<div class="cfg-sec"><div class="cfg-h">${esc(title)}</div><table class="cfg-t"><tbody>${rows}</tbody></table></div>`;
  const row = (k,v) => `<tr><td class="cfg-k">${esc(k)}</td><td class="cfg-v">${v}</td></tr>`;

  el.innerHTML = warn +
    '<div class="cfg-grid">' +
    section('Searching for these roles', row('Keywords', chips(s.keywords)) +
        row('Locations', chips(s.locations)) +
        row('Posted within', esc(s.date_posted||'any')) +
        row('Work type', chips(s.remote)) +
        row('Experience', chips(s.experience_levels)) +
        row('Job types', chips(s.job_types)) +
        row('Easy Apply only', yesno(s.easy_apply_only)) +
        row('Pages/search', esc(s.max_pages)) +
        row('Min match score', esc(s.min_match_score)) +
        row('Blacklist companies', chips(s.blacklist_companies)) +
        row('Blacklist JD words', chips(s.blacklist_keywords))
    ) +
    section('Applicant', row('Name', esc(p.name)) + row('Email', esc(p.email)) + row('Location', esc(p.location))) +
    section('Run mode', row('Dry run', yesno(bot.dry_run)) + row('Max applications', esc(bot.max_applications))) +
    section('Beyond Easy Apply (external ATS)',
        row('Enabled', yesno(ext.enabled)) + row('Auto-submit external', yesno(ext.submit)) + row('Max pages', esc(ext.max_pages))) +
    section('Resume', row('Mode', esc(r.mode)) + row('Your resume file', esc(r.static_resume_path||'(generated per job)'))) +
    section('Salary band', row('Min', sal.min_annual?money(sal.min_annual):'—') + row('Max', sal.max_annual?money(sal.max_annual):'—')) +
    section('Experience-fit gate', row('Enabled', yesno(fit.enabled)) + row('Min score to apply', esc(fit.min_score||0)) + row('Skip verdicts', chips(fit.skip_recommendations))) +
    section('AI', row('Provider', esc(llm.provider)) + row('Field model', esc(llm.field_model)) + row('Resume model', esc(llm.resume_model)) + row('Key configured', yesno(llm.configured))) +
    '</div>' +
    '<div class="muted" style="margin-top:12px;font-size:12px;">Edit these in <code>config/settings.yaml</code> (and <code>config/*.local.yaml</code>), or override roles/locations on the CLI with <code>-k</code>/<code>-l</code>. Restart the run to apply.</div>';
}

function renderRuns(rows){
  if(!rows.length){ document.getElementById('runs').innerHTML='<div class="empty">No runs recorded yet.</div>'; return; }
  const body = rows.map(r=>`<tr>
    <td>#${r.id}</td>
    <td>${r.dry_run?'<span class="pill s-dry_run">dry-run</span>':'<span class="pill s-applied">LIVE</span>'}</td>
    <td>${esc(r.status)}</td>
    <td>${num(r.listings_found)} / ${num(r.listings_after_filter)}</td>
    <td>${num(r.applied_count)}</td>
    <td>${num(r.failed_count)}</td>
    <td>${num(r.skipped_count)}</td>
    <td class="muted">${esc((r.started_at||'').slice(0,16).replace('T',' '))}</td>
  </tr>`).join('');
  document.getElementById('runs').innerHTML =
    `<table><thead><tr><th>Run</th><th>Mode</th><th>Status</th><th>Found/Filt</th><th>Applied</th><th>Failed</th><th>Skipped</th><th>Started</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderErrors(rows){
  if(!rows.length){ document.getElementById('errors').innerHTML='<div class="empty">No errors recorded 🎉</div>'; return; }
  const body = rows.map(r=>`<tr>
    <td class="muted">${esc((r.occurred_at||'').slice(0,19).replace('T',' '))}</td>
    <td>${esc(r.stage)}</td>
    <td>${esc(r.error_type)}</td>
    <td>${esc(r.company)} ${r.job_title?'· '+esc(r.job_title):''}</td>
    <td>${esc(r.message)}</td>
  </tr>`).join('');
  document.getElementById('errors').innerHTML =
    `<table><thead><tr><th>When</th><th>Stage</th><th>Type</th><th>Job</th><th>Message</th></tr></thead><tbody>${body}</tbody></table>`;
}

function showTab(t){
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('active', el.dataset.tab===t));
  ['apps','fit','feedback','config','runs','errors'].forEach(id=>document.getElementById(id).classList.toggle('hidden', id!==t));
}

loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>
"""
