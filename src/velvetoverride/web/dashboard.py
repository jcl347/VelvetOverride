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
            stats["nav_assists"] = db.count_errors_by_stage("nav_assist")
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

    @app.route("/api/cover_letters")
    def api_cover_letters() -> Any:
        # The narrative text the bot generated per job (cover letters, summaries,
        # motivation answers) so you can read and reuse them.
        db = _db(app.config["DB_PATH"])
        try:
            rows = db.get_cover_letters()
            return jsonify([{
                "company": r.get("company", ""),
                "job_title": r.get("job_title", ""),
                "job_url": r.get("job_url", ""),
                "field": r.get("question_text", ""),
                "source": r.get("answer_source", ""),
                "text": r.get("answer_given", ""),
                "applied_at": r.get("applied_at", ""),
            } for r in rows])
        finally:
            db.close()

    @app.route("/api/resumes")
    def api_resumes() -> Any:
        # The resumes the bot uses: the active setting, every generated PDF on
        # disk, and which resume each application actually used.
        from datetime import datetime, timezone
        resume_root = app.config["RESUME_DIR"]
        mode, static_name, static_exists = "tailored", "", False
        try:
            from velvetoverride.utils.config import load_config
            c = load_config()
            mode = c.resume_config.get("mode", "tailored")
            sp = c.resume_config.get("static_resume_path", "")
            if sp:
                static_name = Path(sp).name
                p = Path(sp)
                if not p.is_absolute():
                    p = Path.cwd() / p
                static_exists = p.resolve().exists()
        except Exception:
            pass
        files = []
        if resume_root.exists():
            for p in sorted(resume_root.glob("*.pdf"),
                            key=lambda x: x.stat().st_mtime, reverse=True)[:300]:
                st = p.stat()
                files.append({
                    "name": p.name,
                    "size_kb": round(st.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                    .isoformat(),
                })
        db = _db(app.config["DB_PATH"])
        try:
            used = []
            for a in db.get_applications(limit=500):
                if a.resume_version:
                    used.append({
                        "company": a.company, "job_title": a.job_title,
                        "resume": Path(a.resume_version).name,
                        "applied_at": a.applied_at,
                    })
        finally:
            db.close()
        return jsonify({"mode": mode, "static_name": static_name,
                        "static_available": static_exists,
                        "files": files, "used": used})

    @app.route("/resume-static")
    def resume_static_file():
        # Serve the configured static resume (it lives outside data/resumes/,
        # e.g. the repo root). Only ever the exact configured path — no user
        # input, so no traversal risk.
        try:
            from velvetoverride.utils.config import load_config
            sp = load_config().resume_config.get("static_resume_path", "")
        except Exception:
            sp = ""
        if not sp:
            abort(404)
        p = Path(sp)
        if not p.is_absolute():
            p = Path.cwd() / p
        p = p.resolve()
        if not p.exists() or p.suffix.lower() not in (".pdf", ".doc", ".docx"):
            abort(404)
        return send_file(str(p))

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
        if inside and target.exists() and target.is_file():
            return send_file(str(target))
        # In static mode the résumé used lives OUTSIDE RESUME_DIR (e.g. repo
        # root), so per-application links like /resume/<static-basename> would
        # 404. Fall back to the configured static résumé when the basename
        # matches it (exact configured path — no user-controlled traversal).
        try:
            from velvetoverride.utils.config import load_config
            sp = load_config().resume_config.get("static_resume_path", "")
        except Exception:
            sp = ""
        if sp:
            p = Path(sp)
            if not p.is_absolute():
                p = Path.cwd() / p
            p = p.resolve()
            if (p.name == Path(name).name and p.exists() and p.is_file()
                    and p.suffix.lower() in (".pdf", ".doc", ".docx")):
                return send_file(str(p))
        abort(404)

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
    --bg: #0b0d0f; --panel: #131519; --panel2: #1a1d22; --text: #e8e4dc;
    --muted: #8f8a7e; --accent: #d9663d; --accent-hi: #eb8560; --accent-soft: rgba(217,102,61,.13);
    --border: #23262d; --border2: #2d313a;
    --good: #6bbf7b; --warn: #d9a441; --bad: #d9635f;
    --mono: "SF Mono","JetBrains Mono","Fira Code",ui-monospace,Menlo,Consolas,monospace;
    --sans: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { margin: 0; font-family: var(--sans); background: var(--bg); color: var(--text);
    -webkit-font-smoothing: antialiased; }
  ::selection { background: var(--accent-soft); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 6px; }

  /* Preloader — a brief choreographed reveal (fades out once content is ready) */
  #preloader { position: fixed; inset: 0; z-index: 9999; background: var(--bg);
    display: flex; align-items: center; justify-content: center;
    transition: opacity .5s ease, visibility .5s ease; }
  #preloader.done { opacity: 0; visibility: hidden; }
  #preloader .pl { font-family: var(--mono); font-size: 12px; letter-spacing: .35em;
    text-transform: uppercase; color: var(--muted); }
  #preloader .pl b { color: var(--accent); font-weight: 600; }

  header { padding: 22px 34px; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
  header h1 { margin: 0; font-size: 17px; font-weight: 650; letter-spacing: .02em; }
  header h1 .v { color: var(--accent); }
  header .kicker { font-family: var(--mono); font-size: 10.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .26em; margin-left: 12px; }
  header .tools { display: flex; align-items: center; gap: 18px; }
  .search { background: var(--panel); border: 1px solid var(--border2); border-radius: 8px;
    color: var(--text); padding: 8px 13px; font-size: 13px; width: 200px; outline: none;
    font-family: var(--sans); transition: border-color .2s ease, width .25s ease, background .2s ease; }
  .search::placeholder { color: var(--muted); }
  .search:focus { border-color: var(--accent); width: 250px; background: var(--panel2); }
  .muted { color: var(--muted); }
  .wrap { padding: 32px 34px 64px; max-width: 1240px; margin: 0 auto; }

  /* Editorial stat blocks — hairline grid, numbered, count-up values */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); gap: 1px;
    background: var(--border); border: 1px solid var(--border); border-radius: 14px;
    overflow: hidden; margin-bottom: 36px; }
  .card { position: relative; background: var(--panel); padding: 18px 18px 18px 20px;
    transition: background .25s ease; }
  .card:hover { background: var(--panel2); }
  /* A semantic rail marks the metrics that matter (accent = headline, green/amber/
     red = outcomes); it extends on hover. Neutral tiles get none. */
  .card.tone::before { content: ""; position: absolute; left: 0; top: 16px; bottom: 16px; width: 2px;
    border-radius: 2px; opacity: .85;
    transition: top .3s cubic-bezier(.22,1,.36,1), bottom .3s cubic-bezier(.22,1,.36,1), opacity .25s ease; }
  .card.tone:hover::before { top: 10px; bottom: 10px; opacity: 1; }
  .card.tone-accent::before { background: var(--accent); }
  .card.tone-good::before   { background: var(--good); }
  .card.tone-bad::before    { background: var(--bad); }
  .card.tone-warn::before   { background: var(--warn); }
  .card .label { font-family: var(--mono); font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .14em; display: block; }
  .card .value { font-size: 30px; font-weight: 680; margin-top: 12px;
    font-variant-numeric: tabular-nums; letter-spacing: -.01em;
    transition: transform .25s cubic-bezier(.22,1,.36,1); }
  .card:hover .value { transform: translateX(3px); }
  .card .value.accent { color: var(--accent); }
  .card .value.good   { color: var(--good); }
  .card .value.bad    { color: var(--bad); }
  .card .value.warn   { color: var(--warn); }

  /* Nav — Swiss-magazine numbered tabs with an accent underline */
  .tabs { display: flex; gap: 2px; margin-bottom: 24px; flex-wrap: wrap; border-bottom: 1px solid var(--border); }
  .tab { padding: 11px 15px 13px; cursor: pointer; font-size: 13.5px; color: var(--muted);
    border-bottom: 2px solid transparent; margin-bottom: -1px;
    display: flex; align-items: baseline; gap: 8px; transition: color .2s ease, border-color .2s ease; }
  .tab .n { font-family: var(--mono); font-size: 10px; color: var(--border2); transition: color .2s ease; }
  .tab:hover { color: var(--text); }
  .tab:hover .n { color: var(--muted); }
  .tab.active { color: var(--text); border-color: var(--accent); }
  .tab.active .n { color: var(--accent); }

  .panel-in { animation: fade .35s ease; }
  @keyframes fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  table { width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  th, td { text-align: left; padding: 11px 14px; font-size: 13px; border-bottom: 1px solid var(--border); }
  th { font-family: var(--mono); color: var(--muted); text-transform: uppercase; font-size: 10.5px;
    letter-spacing: .11em; font-weight: 500; background: var(--panel2); }
  tbody tr { transition: background .15s ease; }
  tbody tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--accent-soft); }
  a { color: var(--accent); text-decoration: none; transition: color .15s ease; }
  a:hover { color: var(--accent-hi); }
  .pill { padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; display: inline-block; }
  .s-applied { background: rgba(107,191,123,.14); color: var(--good); }
  .s-dry_run { background: var(--accent-soft); color: var(--accent); }
  .s-needs_review { background: rgba(217,164,65,.14); color: var(--warn); }
  .s-failed { background: rgba(217,99,95,.14); color: var(--bad); }
  .s-skipped { background: var(--panel2); color: var(--muted); }
  .fit-good { color: var(--good); font-weight: 700; }
  .fit-mid  { color: var(--warn); font-weight: 700; }
  .fit-bad  { color: var(--bad); font-weight: 700; }
  .fit-legend { background: var(--panel); border: 1px solid var(--border); border-left: 2px solid var(--accent);
    border-radius: 10px; padding: 12px 16px; margin-bottom: 14px; font-size: 13px; }
  .bar-wrap { display: inline-block; width: 70px; height: 5px; background: var(--panel2);
    border-radius: 3px; overflow: hidden; vertical-align: middle; }
  .bar { height: 100%; border-radius: 3px; }
  .bar-num { font-weight: 700; margin-left: 8px; font-size: 12px; font-variant-numeric: tabular-nums; }
  .score-cell { white-space: nowrap; }
  .gaps { color: #c9c4b8; font-size: 12px; max-width: 260px; }
  .reason { color: var(--muted); font-size: 12px; max-width: 300px; }
  .field-cell { color: #c9c4b8; font-size: 12.5px; max-width: 260px; }
  .prob-cell { max-width: 240px; }
  .flagged-row td { background: rgba(217,99,95,0.07); }
  .prob-flag { display: inline-block; background: rgba(217,99,95,0.16); color: #e79088;
    border: 1px solid rgba(217,99,95,0.35); border-radius: 6px; padding: 2px 7px; font-size: 12px; }
  .src-pill { display: inline-block; background: var(--panel2); border: 1px solid var(--border2);
    border-radius: 6px; padding: 1px 8px; font-size: 11.5px; color: var(--muted); font-family: var(--mono); }
  .letters-wrap { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
  .letter-card { position: relative; background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 15px 17px; overflow: hidden;
    transition: transform .3s cubic-bezier(.22,1,.36,1), border-color .3s ease, box-shadow .3s ease; }
  /* Strong, professional hover on a question response: lift, accent edge, glow,
     and an accent rail that sweeps in from the left. */
  .letter-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
    background: var(--accent); transform: scaleY(0); transform-origin: top;
    transition: transform .35s cubic-bezier(.22,1,.36,1); }
  .letter-card:hover { transform: translateY(-4px); border-color: var(--accent);
    box-shadow: 0 16px 40px rgba(217,102,61,.15), 0 3px 12px rgba(0,0,0,.35); }
  .letter-card:hover::before { transform: scaleY(1); }
  .letter-card:hover .letter-text { border-color: var(--border2); }
  .letter-card:hover .copy-btn { color: var(--accent); border-color: var(--accent); }
  .letter-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 10px; }
  .letter-co { font-weight: 700; font-size: 14px; }
  .letter-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; flex-shrink: 0; }
  .letter-field { font-size: 11.5px; color: var(--accent); max-width: 160px; text-align: right; font-family: var(--mono); }
  .letter-text { white-space: pre-wrap; font-size: 13px; line-height: 1.55; color: #d8d3c8;
    max-height: 160px; overflow-y: auto; padding: 10px 12px; background: var(--bg);
    border-radius: 8px; border: 1px solid var(--border); }
  .copy-btn { margin-top: 10px; font-size: 12px; background: transparent; color: var(--muted);
    border: 1px solid var(--border2); border-radius: 6px; padding: 4px 12px; cursor: pointer;
    font-family: var(--mono); transition: all .18s ease; }
  .copy-btn:hover { color: var(--accent); border-color: var(--accent); }
  .hidden { display: none; }
  .refresh { font-family: var(--mono); font-size: 11px; color: var(--muted); cursor: pointer;
    letter-spacing: .04em; transition: color .2s ease; }
  .refresh:hover { color: var(--accent); }
  .empty { padding: 46px 30px; text-align: center; color: var(--muted); background: var(--panel);
    border: 1px dashed var(--border2); border-radius: 12px; font-size: 13.5px; }
  .cfg-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
  .cfg-sec { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 15px 17px; }
  .cfg-h { font-family: var(--mono); font-size: 10.5px; text-transform: uppercase; letter-spacing: .14em;
    color: var(--accent); margin-bottom: 10px; font-weight: 600; }
  .cfg-t { background: transparent; border: none; }
  .cfg-t td { border-bottom: 1px solid var(--border); padding: 7px 4px; vertical-align: top; }
  .cfg-t tr:hover td { background: transparent; }
  .cfg-k { color: var(--muted); width: 45%; font-size: 12.5px; }
  .cfg-v { font-size: 12.5px; }
  .chip { display: inline-block; background: var(--panel2); border: 1px solid var(--border2);
    border-radius: 999px; padding: 2px 10px; font-size: 11.5px; margin: 1px 0; }
  code { background: var(--panel2); padding: 1px 6px; border-radius: 4px; font-size: 12px;
    font-family: var(--mono); color: #d8d3c8; }
  h3 { font-family: var(--mono); font-size: 11.5px; text-transform: uppercase; letter-spacing: .1em;
    color: var(--muted); font-weight: 500; }

  /* Cmd+K command palette */
  #cmdk { position: fixed; inset: 0; z-index: 500; display: none; align-items: flex-start;
    justify-content: center; background: rgba(0,0,0,.55); backdrop-filter: blur(3px); padding-top: 13vh; }
  #cmdk.open { display: flex; animation: fade .16s ease; }
  #cmdk .box { width: min(560px, 92vw); background: var(--panel); border: 1px solid var(--border2);
    border-radius: 14px; overflow: hidden; box-shadow: 0 24px 64px rgba(0,0,0,.55); }
  #cmdk input { width: 100%; border: none; background: transparent; color: var(--text);
    padding: 16px 18px; font-size: 15px; outline: none; border-bottom: 1px solid var(--border); font-family: var(--sans); }
  #cmdk .results { max-height: 340px; overflow-y: auto; padding: 6px; }
  #cmdk .res { padding: 10px 12px; border-radius: 8px; cursor: pointer; display: flex;
    align-items: baseline; gap: 11px; font-size: 13.5px; }
  #cmdk .res .n { font-family: var(--mono); font-size: 10px; color: var(--muted); }
  #cmdk .res.sel { background: var(--accent-soft); }
  #cmdk .res .sub { color: var(--muted); font-size: 12px; margin-left: auto; }
  #cmdk .hint { padding: 9px 15px; border-top: 1px solid var(--border); font-family: var(--mono);
    font-size: 10.5px; color: var(--muted); display: flex; gap: 16px; }
  kbd { font-family: var(--mono); background: var(--panel2); border: 1px solid var(--border2);
    border-radius: 4px; padding: 1px 6px; font-size: 10px; }
</style>
</head>
<body>
<div id="preloader"><div class="pl"><b>Velvet</b>Override</div></div>
<header>
  <h1><span class="v">Velvet</span>Override<span class="kicker">application tracker</span></h1>
  <div class="tools">
    <input id="search" class="search" type="text" placeholder="Search companies…" autocomplete="off" spellcheck="false">
    <span class="refresh" onclick="loadAll()">↻ sync&nbsp;·&nbsp;<span id="ts"></span>&nbsp;·&nbsp;<kbd>⌘K</kbd></span>
  </div>
</header>
<div class="wrap">
  <div id="errbanner" style="display:none;background:rgba(217,99,95,.12);color:#e79088;border:1px solid rgba(217,99,95,.3);border-radius:10px;padding:12px 16px;margin-bottom:16px;"></div>
  <div class="cards" id="cards"></div>
  <div class="tabs">
    <div class="tab active" data-tab="apps" onclick="showTab('apps')"><span class="n">01</span>Applications</div>
    <div class="tab" data-tab="letters" onclick="showTab('letters')"><span class="n">02</span>Questions</div>
    <div class="tab" data-tab="fit" onclick="showTab('fit')"><span class="n">03</span>Experience fit</div>
    <div class="tab" data-tab="feedback" onclick="showTab('feedback')"><span class="n">04</span>Field feedback</div>
    <div class="tab" data-tab="resumes" onclick="showTab('resumes')"><span class="n">05</span>Résumés</div>
    <div class="tab" data-tab="config" onclick="showTab('config')"><span class="n">06</span>Config &amp; search</div>
    <div class="tab" data-tab="runs" onclick="showTab('runs')"><span class="n">07</span>Runs</div>
    <div class="tab" data-tab="errors" onclick="showTab('errors')"><span class="n">08</span>Errors</div>
  </div>
  <div id="apps"></div>
  <div id="fit" class="hidden"></div>
  <div id="feedback" class="hidden"></div>
  <div id="letters" class="hidden"></div>
  <div id="resumes" class="hidden"></div>
  <div id="config" class="hidden"></div>
  <div id="runs" class="hidden"></div>
  <div id="errors" class="hidden"></div>
  <footer style="margin-top:44px;padding-top:20px;border-top:1px solid var(--border);font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:.04em;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;">
    <span>VelvetOverride — automated application tracker</span>
    <span>localhost · read-only · auto-syncs every 15s</span>
  </footer>
</div>
<div id="cmdk"><div class="box">
  <input id="cmdk-input" type="text" placeholder="Jump to a section or find a company…" autocomplete="off" spellcheck="false">
  <div class="results" id="cmdk-results"></div>
  <div class="hint"><span><kbd>↑</kbd> <kbd>↓</kbd> navigate</span><span><kbd>↵</kbd> open</span><span><kbd>esc</kbd> close</span></div>
</div></div>
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
  const [sum, apps, fit, feedback, letters, resumes, runs, errors, cfg] = await Promise.all([
    getJSON('/api/summary'), getJSON('/api/applications'), getJSON('/api/fit'),
    getJSON('/api/feedback'), getJSON('/api/cover_letters'), getJSON('/api/resumes'),
    getJSON('/api/runs'), getJSON('/api/errors'), getJSON('/api/config'),
  ]);
  const banner = document.getElementById('errbanner');
  if (sum == null && apps == null){
    banner.style.display = 'block';
    banner.textContent = 'Cannot reach the tracker API. Is the dashboard server running?';
    return;
  }
  banner.style.display = 'none';
  window._apps = apps || [];
  renderCards(sum || {});
  renderApps(apps || []);
  if (window.filterApps) filterApps();   // keep any active company filter applied
  renderFit(fit || [], (sum && sum.fit) || {});
  renderFeedback(feedback || []);
  renderLetters(letters || []);
  renderResumes(resumes || {});
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
  // [label, value, tone]  — tone marks the metrics that matter: accent = headline,
  // good/warn/bad = outcomes, '' = neutral. Integer values count up.
  const cards = [
    ['Jobs processed', s.total_applications||0, ''],
    ['Applied / submitted', applied, 'accent'],
    ['Failed', by.failed||0, 'bad'],
    ['Skipped · dupes', by.skipped||0, ''],
    ['Errors logged', s.errors_total||0, 'bad'],
    ['AI nav resolutions', s.nav_assists||0, ''],
    ['Avg experience fit', f.avg_score != null ? f.avg_score : null, 'accent'],
    ['Strong fits', rec.apply||0, 'good'],
    ['Poor fits', rec.skip||0, 'warn'],
  ];
  document.getElementById('cards').innerHTML = cards.map((c)=>{
    const isCount = Number.isInteger(c[1]);
    const disp = c[1]==null ? '—' : (isCount ? '0' : esc(String(c[1])));
    const cardCls = c[2] ? ' tone tone-'+c[2] : '';
    const valCls = c[2] ? ' '+c[2] : '';
    return `<div class="card${cardCls}">`
      + `<span class="label">${esc(c[0])}</span>`
      + `<div class="value${valCls}"${isCount?` data-count="${c[1]}"`:''}>${disp}</div></div>`;
  }).join('');
  animateCounts();
}

// Odometer-style count-up on first load; snaps to the value on later refreshes.
function animateCounts(){
  document.querySelectorAll('#cards .value[data-count]').forEach(el=>{
    const target = parseInt(el.dataset.count, 10) || 0;
    if (window._statsAnimated){ el.textContent = num(target); return; }
    const dur = 850, t0 = performance.now();
    requestAnimationFrame(function tick(now){
      const p = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);        // easeOutCubic — fast start, long settle
      el.textContent = num(Math.round(target * e));
      if (p < 1) requestAnimationFrame(tick); else el.textContent = num(target);
    });
  });
  window._statsAnimated = true;
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

function copyText(btn){
  const t = btn.parentElement.querySelector('.letter-text').textContent;
  navigator.clipboard.writeText(t).then(()=>{ btn.textContent='Copied'; setTimeout(()=>btn.textContent='Copy', 1500); });
}
window.copyText = copyText;

function renderResumes(d){
  const el = document.getElementById('resumes');
  d = d || {};
  const files = d.files || [];
  const used = d.used || [];
  // Which resume is active
  let active;
  if (d.mode === 'static'){
    active = d.static_available
      ? `<b>Static</b> — every application uploads <a href="/resume-static" target="_blank">${esc(d.static_name)}</a>`
      : `<b>Static</b> — but no valid file at the configured path (uploads will fall back)`;
  } else {
    active = `<b>Tailored</b> — a per-job résumé is generated from your profile for each application`;
  }
  const legend = `<div class="fit-legend">Résumé mode: ${active}
      <div class="muted" style="margin-top:4px;">${files.length} generated PDFs on disk &middot; ${used.length} applications have a résumé on file. Click any to open.</div></div>`;

  const usedBody = used.slice(0,300).map(r=>`<tr>
      <td>${esc(r.company)}</td><td>${esc(r.job_title)}</td>
      <td><a href="/resume/${encodeURIComponent(r.resume)}" target="_blank">${esc(r.resume.slice(0,44))}</a></td>
      <td class="muted">${esc((r.applied_at||'').slice(0,10))}</td></tr>`).join('');
  const usedTable = used.length
    ? `<h3 style="margin:18px 0 8px;">Résumé used per application</h3>
       <table><thead><tr><th>Company</th><th>Role</th><th>Résumé (click to open)</th><th>Applied</th></tr></thead><tbody>${usedBody}</tbody></table>`
    : '';

  const fileBody = files.slice(0,300).map(f=>`<tr>
      <td><a href="/resume/${encodeURIComponent(f.name)}" target="_blank">${esc(f.name.slice(0,60))}</a></td>
      <td class="muted">${f.size_kb} KB</td>
      <td class="muted">${esc((f.modified||'').slice(0,10))}</td></tr>`).join('');
  const fileTable = files.length
    ? `<h3 style="margin:18px 0 8px;">All generated résumés (${files.length})</h3>
       <table><thead><tr><th>File</th><th>Size</th><th>Created</th></tr></thead><tbody>${fileBody}</tbody></table>`
    : '<div class="empty">No generated résumés on disk yet.</div>';

  if (!files.length && !used.length && !d.static_available){
    el.innerHTML = legend + '<div class="empty">No résumés to show yet. In tailored mode they appear here after the first application; in static mode set resume.static_resume_path.</div>';
    return;
  }
  el.innerHTML = legend + usedTable + fileTable;
}

function renderLetters(rows){
  const el = document.getElementById('letters');
  if(!rows.length){
    el.innerHTML = '<div class="empty">No question responses yet. When a job asks an open-ended question &mdash; a cover letter, summary, or &ldquo;why are you interested&rdquo; &mdash; the bot writes an answer from your background + the JD and saves it here to read and reuse.</div>';
    return;
  }
  const legend = `<div class="fit-legend"><b>${rows.length}</b> question responses saved
      <div class="muted" style="margin-top:4px;">Answers the bot wrote to open-ended application questions (cover letters, summaries, motivation). Hover a card to focus it; use Copy to reuse the text.</div></div>`;
  const cards = rows.map((r,i)=>`<div class="letter-card">
      <div class="letter-head">
        <div>
          <div class="letter-co">${esc(r.company)||'&mdash;'}</div>
          <div class="muted">${r.job_url?`<a href="${esc(r.job_url)}" target="_blank">${esc(r.job_title)}<\/a>`:esc(r.job_title)}</div>
        </div>
        <div class="letter-meta">
          <span class="letter-field">${esc(r.field)}</span>
          <span class="src-pill">${esc(r.source||'')}</span>
        </div>
      </div>
      <div class="letter-text">${esc(r.text)}</div>
      <button class="copy-btn" onclick="copyText(this)">Copy</button>
    </div>`).join('');
  el.innerHTML = legend + `<div class="letters-wrap">${cards}</div>`;
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
    ? '<div style="background:rgba(217,164,65,.12);color:var(--warn);border:1px solid rgba(217,164,65,.3);border-radius:8px;padding:9px 13px;margin-bottom:12px;font-size:13px;">Profile is still the placeholder — copy <code>config/profile.yaml</code> → <code>config/profile.local.yaml</code> and edit it before applying.</div>'
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
  if(!rows.length){ document.getElementById('errors').innerHTML='<div class="empty">No errors recorded.</div>'; return; }
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
  ['apps','fit','feedback','letters','resumes','config','runs','errors'].forEach(id=>{
    const el = document.getElementById(id);
    el.classList.toggle('hidden', id!==t);
    if (id===t){ el.classList.remove('panel-in'); void el.offsetWidth; el.classList.add('panel-in'); }
  });
}
window.showTab = showTab;

/* ── Cmd+K command palette: jump to a section, or find a company ───────────── */
const CMDK_TABS = [
  ['apps','01 · Applications'],['letters','02 · Questions'],['fit','03 · Experience fit'],
  ['feedback','04 · Field feedback'],['resumes','05 · Résumés'],['config','06 · Config & search'],
  ['runs','07 · Runs'],['errors','08 · Errors'],
];
let _cmdkItems = [], _cmdkSel = 0;
function cmdkCompanies(){
  const seen = new Set(), out = [];
  for (const r of (window._apps || [])){
    const key = (r.company||'').toLowerCase();
    if (!r.company || seen.has(key)) continue;
    seen.add(key);
    out.push({label: r.company, sub: r.job_title||'', tab: 'apps'});
  }
  return out;
}
function cmdkRender(q){
  q = (q||'').toLowerCase().trim();
  const tabs = CMDK_TABS.map(([id,label])=>({label, sub:'section', tab:id}));
  let items = tabs.concat(cmdkCompanies());
  if (q) items = items.filter(it => (it.label+' '+it.sub).toLowerCase().includes(q));
  _cmdkItems = items.slice(0, 40); _cmdkSel = 0;
  const box = document.getElementById('cmdk-results');
  box.innerHTML = _cmdkItems.map((it,i)=>
    `<div class="res${i===0?' sel':''}" data-i="${i}"><span class="n">${it.sub==='section'?'§':'▸'}</span>`
    + `<span>${esc(it.label)}</span><span class="sub">${esc(it.sub==='section'?'section':it.sub)}</span></div>`).join('')
    || '<div class="res muted" style="cursor:default;">No matches</div>';
  box.querySelectorAll('.res[data-i]').forEach(el=>{
    el.onclick = ()=>{ _cmdkSel = +el.dataset.i; cmdkGo(); };
  });
}
function cmdkMove(d){
  if (!_cmdkItems.length) return;
  _cmdkSel = (_cmdkSel + d + _cmdkItems.length) % _cmdkItems.length;
  document.querySelectorAll('#cmdk .res[data-i]').forEach(el=>el.classList.toggle('sel', +el.dataset.i===_cmdkSel));
  const sel = document.querySelector('#cmdk .res.sel'); if (sel) sel.scrollIntoView({block:'nearest'});
}
function cmdkGo(){
  const it = _cmdkItems[_cmdkSel]; if (!it) return;
  if (it.sub && it.sub !== 'section'){          // a company → filter the applications table
    const s = document.getElementById('search'); s.value = it.label; window._q = it.label.toLowerCase();
  }
  showTab(it.tab); filterApps(); cmdkClose();
}

/* Live company filter on the Applications table (survives the 15s refresh). */
window._q = '';
function filterApps(){
  const q = window._q || '';
  document.querySelectorAll('#apps tbody tr').forEach(tr=>{
    tr.style.display = (!q || tr.textContent.toLowerCase().includes(q)) ? '' : 'none';
  });
}
window.filterApps = filterApps;
document.getElementById('search').addEventListener('input', e=>{
  window._q = e.target.value.toLowerCase().trim();
  showTab('apps'); filterApps();
});
function cmdkOpen(){
  const m = document.getElementById('cmdk'); m.classList.add('open');
  const inp = document.getElementById('cmdk-input'); inp.value=''; cmdkRender(''); inp.focus();
}
function cmdkClose(){ document.getElementById('cmdk').classList.remove('open'); }
document.addEventListener('keydown', e=>{
  if ((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==='k'){ e.preventDefault(); cmdkOpen(); return; }
  const open = document.getElementById('cmdk').classList.contains('open');
  if (!open) return;
  if (e.key==='Escape') cmdkClose();
  else if (e.key==='ArrowDown'){ e.preventDefault(); cmdkMove(1); }
  else if (e.key==='ArrowUp'){ e.preventDefault(); cmdkMove(-1); }
  else if (e.key==='Enter'){ e.preventDefault(); cmdkGo(); }
});
document.getElementById('cmdk-input').addEventListener('input', e=>cmdkRender(e.target.value));
document.getElementById('cmdk').addEventListener('click', e=>{ if (e.target.id==='cmdk') cmdkClose(); });

loadAll().then(()=>{
  const pl = document.getElementById('preloader');
  if (pl) setTimeout(()=>pl.classList.add('done'), 260);
});
setInterval(loadAll, 15000);
</script>
</body>
</html>
"""
