#!/usr/bin/env python3
"""
IEEE FSM Student Branch — local content admin server.

Run this from inside your local repo (same folder as index.html), or point
INDEX_HTML_PATH below at it. It serves a small dashboard at
http://127.0.0.1:5055 that reads your real index.html, lets you add/edit/
reorder activities, units, and hero stats, uploads images straight into your
repo's image folders, and writes changes directly back into index.html.

Nothing is pushed to git automatically — review the diff and push yourself.

    pip install flask --break-system-packages
    python admin_server.py

"""
import os
import re
import html as html_lib
import shutil
import datetime
import subprocess
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename

# ============================================================
# CONFIG — edit these to match your repo layout
# ============================================================
INDEX_HTML_PATH = "index.html"       # path to your site's index.html
ACTIVITIES_IMG_DIR = "Act_Images"    # folder used by <img src="Act_Images/...">
UNITS_IMG_DIR = "Unit_Images"        # folder for unit logos (adjust if yours differs)
REPO_DIR = "."                       # folder containing your git repo — usually same folder as this script
PORT = 5055
DEFAULT_YEAR = str(datetime.datetime.now().year)

app = Flask(__name__)


# ============================================================
# Low-level helpers: find a container's inner HTML by class,
# using simple tag-depth counting (no HTML parser, so nothing
# else in the file is ever touched or re-serialized).
# ============================================================
def _span_from_open_match(html, m, tag):
    """Given a regex match object for an opening <tag ...>, return
    (inner_start, inner_end, outer_end) by counting nested tags of the
    same name until the matching close tag is found."""
    open_re = re.compile(r'<' + tag + r'\b[^>]*>')
    close_tag = '</' + tag + '>'
    inner_start = m.end()
    depth = 1
    pos = inner_start
    while depth > 0:
        next_open = open_re.search(html, pos)
        next_close = html.find(close_tag, pos)
        if next_close == -1:
            raise ValueError(f"Could not find matching close for <{tag}> opened at {m.start()}")
        if next_open and next_open.start() < next_close:
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close + len(close_tag)
    inner_end = pos - len(close_tag)
    return inner_start, inner_end, pos


def find_container_span(html, class_name, tag="div"):
    """Return (inner_start, inner_end, outer_end) for the first
    <tag class="...class_name..."> ... </tag> block, matching nested
    tags of the same name by depth."""
    m = re.search(r'<' + tag + r'\b[^>]*class="[^"]*\b' + re.escape(class_name) + r'\b[^"]*"[^>]*>', html)
    if not m:
        return None
    return _span_from_open_match(html, m, tag)


def find_section_span_by_id(html, section_id):
    """Return (inner_start, inner_end, outer_end) for <section id="section_id">...</section>."""
    m = re.search(r'<section\b[^>]*\bid="' + re.escape(section_id) + r'"[^>]*>', html)
    if not m:
        return None
    return _span_from_open_match(html, m, "section")


def list_all_sections(html):
    """Return [{id, label}] for every top-level <section id="..."> in the file,
    in document order, with a friendly label pulled from the first heading
    or eyebrow text inside it if present."""
    out = []
    for m in re.finditer(r'<section\b[^>]*\bid="([^"]+)"[^>]*>', html):
        section_id = m.group(1)
        span = _span_from_open_match(html, m, "section")
        inner = html[span[0]:span[1]]
        label = text_of(r'<h2[^>]*>(.*?)</h2>', inner, "") or \
                text_of(r'<span class="eyebrow"[^>]*>(.*?)</span>', inner, "") or \
                section_id
        out.append({"id": section_id, "label": label})
    return out


def top_level_blocks(html, class_name, tag="div"):
    """Return list of (start, end) spans for each top-level <tag class="class_name">
    block within `html` (does not descend into nested same-class tags)."""
    blocks = []
    pos = 0
    open_re = re.compile(r'<' + tag + r'\b[^>]*class="[^"]*\b' + re.escape(class_name) + r'\b[^"]*"[^>]*>')
    while True:
        m = open_re.search(html, pos)
        if not m:
            break
        start = m.start()
        depth = 1
        scan_pos = m.end()
        any_open_re = re.compile(r'<' + tag + r'\b[^>]*>')
        close_tag = '</' + tag + '>'
        while depth > 0:
            next_open = any_open_re.search(html, scan_pos)
            next_close = html.find(close_tag, scan_pos)
            if next_close == -1:
                raise ValueError(f"Unbalanced <{tag}> for class {class_name}")
            if next_open and next_open.start() < next_close:
                depth += 1
                scan_pos = next_open.end()
            else:
                depth -= 1
                scan_pos = next_close + len(close_tag)
        end = scan_pos
        blocks.append((start, end))
        pos = end
    return blocks


def text_of(pattern, block, default="", unescape=True):
    m = re.search(pattern, block, re.S)
    val = m.group(1).strip() if m else default
    val = re.sub(r'\s+', ' ', val)
    return html_lib.unescape(val) if unescape else val


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def img_src_of(block, default=""):
    """Find the src of the first <img> tag in `block`, regardless of what
    other attributes (id, alt, class, onerror...) come before or after it —
    unlike a naive '<img src="..."' regex, which silently fails the moment
    an attribute like id="..." appears first in real markup."""
    m = re.search(r'<img\b[^>]*>', block, re.S)
    if not m:
        return default
    sm = re.search(r'\bsrc="(.*?)"', m.group(0))
    return html_lib.unescape(sm.group(1)) if sm else default


def year_from_date_string(date_str, fallback=DEFAULT_YEAR):
    """Pull a 4-digit year out of a display date like '08 April 2026' or
    '24 & 25 January 2026'. This is the single source of truth for year —
    nothing else in the app stores year separately, so it can never drift
    out of sync with the date shown."""
    m = re.search(r'(\d{4})', date_str or "")
    return m.group(1) if m else fallback


# ============================================================
# Structural validation — run before every write so a bug in the
# generator can never silently corrupt the file. If this fails,
# nothing is written and the original file is left untouched.
# ============================================================
class ValidationError(Exception):
    pass


def tag_balance_ok(html, tag):
    opens = len(re.findall(r'<' + tag + r'\b[^>]*(?<!/)>', html))
    closes = len(re.findall(r'</' + tag + r'>', html))
    return opens == closes, opens, closes


def validate_html(html, context=""):
    for tag in ("div", "section"):
        ok, opens, closes = tag_balance_ok(html, tag)
        if not ok:
            raise ValidationError(
                f"Refused to save{(' — ' + context) if context else ''}: "
                f"<{tag}> tags are unbalanced ({opens} opening vs {closes} closing). "
                f"Nothing was written; your file is unchanged."
            )
    for required in ('<div class="timeline">', '<div class="units-grid">', "</html>"):
        if required not in html:
            raise ValidationError(
                f"Refused to save{(' — ' + context) if context else ''}: "
                f"expected to still find `{required}` in the result but it's missing. "
                f"Nothing was written; your file is unchanged."
            )


# ============================================================
# Parsing: read current activities / units / stats from file
# ============================================================
def read_index_html():
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


def parse_activities(html):
    span = find_container_span(html, "timeline")
    if not span:
        return []
    inner = html[span[0]:span[1]]
    items = []
    for start, end in top_level_blocks(inner, "tl-item"):
        block = inner[start:end]
        date = text_of(r'<div class="tl-date">(.*?)</div>', block)
        year_m = re.search(r'data-year="(\d{4})"', block)
        items.append({
            "date": date,
            "year": year_m.group(1) if year_m else year_from_date_string(date),
            "title": text_of(r'<h3>(.*?)</h3>', block),
            "desc": text_of(r'<h3>.*?</h3>\s*<p>(.*?)</p>', block),
            "link": text_of(r'<a class="tl-more" href="(.*?)"', block),
            "img": text_of(r'<img\b[^>]*\bsrc="(?:' + re.escape(ACTIVITIES_IMG_DIR) + r'/)?(.*?)"', block, "placeholder.jpg"),
            "raw_html": block,
        })
    return items


def parse_units(html):
    span = find_container_span(html, "units-grid")
    if not span:
        return []
    inner = html[span[0]:span[1]]
    items = []
    for start, end in top_level_blocks(inner, "unit-card"):
        block = inner[start:end]
        color_m = re.search(r'--unit-color:\s*([^;"]+);', block)
        logo_id_m = re.search(r'<img\b[^>]*\bid="([^"]*)"[^>]*\bsrc="', block) or re.search(r'<img\b[^>]*\bsrc="[^"]*"[^>]*\bid="([^"]*)"', block)
        items.append({
            "name": text_of(r'<h3>(.*?)</h3>', block),
            "tag": text_of(r'<span class="tag">(.*?)</span>', block),
            "color": color_m.group(1).strip() if color_m else "#00629B",
            "summary": text_of(r'<h3>.*?</h3>\s*<p>(.*?)</p>', block),
            "details": text_of(r'data-details="(.*?)"', block),
            "website": text_of(r'data-website="(.*?)"', block),
            "logo": text_of(r'<img\b[^>]*\bsrc="(.*?)"', block, "placeholder-logo.svg"),
            "logo_id": logo_id_m.group(1) if logo_id_m else "",
            "raw_html": block,
        })
    return items


def parse_stats(html):
    rows = re.findall(
        r'<div class="hero-stat-row"><span class="label">(.*?)</span><span class="num">(.*?)</span></div>',
        html
    )
    return [{"label": html_lib.unescape(l.strip()), "value": html_lib.unescape(v.strip())} for l, v in rows]


# ============================================================
# Serialization: build HTML blocks from state.
# Every field is read with .get(..., default) rather than [..],
# so a missing/renamed key on the JS side produces a sane fallback
# instead of a hard crash mid-save.
# ============================================================
def build_activity_block(a):
    date = a.get("date", "")
    year = year_from_date_string(date, fallback=a.get("year", DEFAULT_YEAR))
    return f'''          <div class="tl-item" data-year="{esc(year)}">
            <div class="tl-date">{esc(date)}</div>
            <div class="tl-node-col">
              <div class="tl-node"></div>
            </div>
            <div class="tl-card">
              <div>
                <h3>{esc(a.get("title", ""))}</h3>
                <p>{esc(a.get("desc", ""))}</p>
                <a class="tl-more" href="{esc(a.get("link", ""))}" target="_blank" rel="noopener">
                  See more <span class="tl-more-arrow">&rarr;</span>
                </a>
              </div>
              <div class="tl-thumb">
                <img src="{ACTIVITIES_IMG_DIR}/{esc(a.get("img", "placeholder.jpg"))}" alt="{esc(a.get("title", ""))}"
                  onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="ph-icon" style="display:none;"><svg class="icon" style="width:30px;height:30px;">
                    <use href="#i-camera" />
                  </svg></div>
              </div>
            </div>
          </div>'''


def build_unit_block(u):
    logo_id = u.get("logo_id", "")
    logo_id_attr = f'id="{esc(logo_id)}" ' if logo_id else ""
    return f'''          <div class="unit-card reveal" style="--unit-color:{esc(u.get("color", "#00629B"))};"
               data-website="{esc(u.get("website", ""))}"
               data-details="{esc(u.get("details", ""))}">
            <div class="unit-logo-img"><img {logo_id_attr}src="{esc(u.get("logo", "placeholder-logo.svg"))}" alt="{esc(u.get("name", ""))} Logo"
                style="width: 180px; height: auto; padding: 25px;"
                onerror="this.style.display='none';"></div>
            <span class="tag">{esc(u.get("tag", ""))}</span>
            <h3>{esc(u.get("name", ""))}</h3>
            <p>{esc(u.get("summary", ""))}</p>
          </div>'''


def activity_output_html(a):
    """Reuse the original block verbatim if this entry was never edited
    (raw_html still present and truthy) — only rebuild from fields for
    genuinely new or edited entries. This means anything our parser
    doesn't capture (an attribute we don't know about, unusual formatting,
    a future markup change) survives untouched for entries the user
    didn't actually change, instead of being silently dropped on every save."""
    raw = a.get("raw_html")
    return raw if raw else build_activity_block(a)


def unit_output_html(u):
    raw = u.get("raw_html")
    return raw if raw else build_unit_block(u)


def replace_container(html, class_name, new_inner_html):
    span = find_container_span(html, class_name)
    if not span:
        raise ValidationError(f"Could not find a container with class \"{class_name}\" — nothing was changed.")
    inner_start, inner_end, _ = span
    return html[:inner_start] + "\n" + new_inner_html + "\n        " + html[inner_end:]


def replace_stats(html, stats):
    existing_count = len(re.findall(r'<div class="hero-stat-row">', html))
    if len(stats) != existing_count:
        raise ValidationError(
            f"Expected {existing_count} hero stat rows but received {len(stats)} — refusing to save "
            f"to avoid dropping or misaligning a stat. Nothing was changed."
        )
    it = iter(stats)
    def repl(_m):
        s = next(it)
        return f'<div class="hero-stat-row"><span class="label">{esc(s.get("label",""))}</span><span class="num">{esc(s.get("value",""))}</span></div>'
    new_html, n = re.subn(
        r'<div class="hero-stat-row"><span class="label">.*?</span><span class="num">.*?</span></div>',
        repl, html
    )
    return new_html


def backup_index():
    if os.path.exists(INDEX_HTML_PATH):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(INDEX_HTML_PATH, f"{INDEX_HTML_PATH}.bak_{ts}")


def write_index_html_safely(new_html, context=""):
    """Validate before writing, back up, then write. Raises ValidationError
    (caught by the route) instead of ever writing something broken."""
    validate_html(new_html, context=context)
    backup_index()
    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)


# ============================================================
# Routes
# ============================================================
@app.route("/api/state")
def api_state():
    try:
        html = read_index_html()
        return jsonify({
            "activities": parse_activities(html),
            "units": parse_units(html),
            "stats": parse_stats(html),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    file = request.files.get("file")
    target_dir = request.form.get("dir", ACTIVITIES_IMG_DIR)
    if not file or file.filename == "":
        return jsonify({"ok": False, "error": "No file provided"}), 400
    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"ok": False, "error": "That filename isn't valid"}), 400
    os.makedirs(target_dir, exist_ok=True)
    save_path = os.path.join(target_dir, filename)
    file.save(save_path)
    return jsonify({"ok": True, "filename": filename, "path": save_path})


@app.route("/api/save", methods=["POST"])
def api_save():
    try:
        data = request.get_json()
        html = read_index_html()

        activities_html = "\n\n".join(activity_output_html(a) for a in data.get("activities", []))
        units_html = "\n\n".join(unit_output_html(u) for u in data.get("units", []))

        new_html = replace_container(html, "timeline", activities_html)
        new_html = replace_container(new_html, "units-grid", units_html)
        new_html = replace_stats(new_html, data.get("stats", []))

        write_index_html_safely(new_html, context="structured save")
        return jsonify({"ok": True, "path": os.path.abspath(INDEX_HTML_PATH)})
    except ValidationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}"}), 500


@app.route("/api/sections")
def api_sections():
    try:
        html = read_index_html()
        return jsonify(list_all_sections(html))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/section/<section_id>", methods=["GET"])
def api_get_section(section_id):
    html = read_index_html()
    span = find_section_span_by_id(html, section_id)
    if not span:
        return jsonify({"ok": False, "error": f"No section with id \"{section_id}\""}), 404
    inner_start, inner_end, _ = span
    return jsonify({"ok": True, "html": html[inner_start:inner_end]})


@app.route("/api/section/<section_id>", methods=["POST"])
def api_save_section(section_id):
    try:
        data = request.get_json()
        new_inner = data.get("html", "")
        html = read_index_html()
        span = find_section_span_by_id(html, section_id)
        if not span:
            return jsonify({"ok": False, "error": f"No section with id \"{section_id}\""}), 404
        inner_start, inner_end, _ = span
        new_html = html[:inner_start] + "\n" + new_inner + "\n      " + html[inner_end:]
        write_index_html_safely(new_html, context=f"section #{section_id}")
        return jsonify({"ok": True, "path": os.path.abspath(INDEX_HTML_PATH)})
    except ValidationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}"}), 500


@app.route("/api/fullfile", methods=["GET"])
def api_get_fullfile():
    return jsonify({"ok": True, "html": read_index_html()})


@app.route("/api/fullfile", methods=["POST"])
def api_save_fullfile():
    try:
        data = request.get_json()
        new_html = data.get("html", "")
        if not new_html.strip():
            return jsonify({"ok": False, "error": "Refusing to save an empty file."}), 400
        write_index_html_safely(new_html, context="full file edit")
        return jsonify({"ok": True, "path": os.path.abspath(INDEX_HTML_PATH)})
    except ValidationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}"}), 500


# ============================================================
# Git — add/commit/push scoped to just the files this tool touches
# (index.html + the two image folders), never a blanket `-A`, so
# nothing unrelated in your repo gets swept in by accident.
# ============================================================
def tracked_paths():
    return [p for p in (INDEX_HTML_PATH, ACTIVITIES_IMG_DIR, UNITS_IMG_DIR) if os.path.exists(p)]


def run_git(args):
    """Run a git command as a real argument list (never a shell string),
    so nothing typed into the commit message box can be interpreted as
    a shell command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args, cwd=REPO_DIR, capture_output=True, text=True
    )
    return result.returncode, result.stdout, result.stderr


@app.route("/api/git/status")
def api_git_status():
    try:
        code, out, err = run_git(["status", "--porcelain", "--"] + tracked_paths())
        if code != 0:
            return jsonify({"ok": False, "error": err.strip() or "git status failed — is this folder actually a git repository?"})
        changed = [line for line in out.splitlines() if line.strip()]
        return jsonify({"ok": True, "changed": changed, "clean": len(changed) == 0})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/git/push", methods=["POST"])
def api_git_push():
    log = []
    try:
        data = request.get_json()
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "Write a commit message first.", "log": ""}), 400

        paths = tracked_paths()
        if not paths:
            return jsonify({"ok": False, "error": "None of the configured paths exist — check INDEX_HTML_PATH/ACTIVITIES_IMG_DIR/UNITS_IMG_DIR at the top of admin_server.py.", "log": ""}), 400

        code, out, err = run_git(["add", "--"] + paths)
        log.append(f"$ git add -- {' '.join(paths)}\n{out}{err}".strip())
        if code != 0:
            return jsonify({"ok": False, "error": "git add failed.", "log": "\n\n".join(log)}), 500

        code, out, err = run_git(["diff", "--cached", "--name-only"])
        if not out.strip():
            return jsonify({"ok": False, "error": "Nothing to commit — index.html and the image folders match what's already committed.", "log": "\n\n".join(log)}), 400

        code, out, err = run_git(["commit", "-m", message])
        log.append(f"$ git commit -m \"{message}\"\n{out}{err}".strip())
        if code != 0:
            return jsonify({"ok": False, "error": "git commit failed.", "log": "\n\n".join(log)}), 500

        code, out, err = run_git(["push"])
        log.append(f"$ git push\n{out}{err}".strip())
        if code != 0:
            return jsonify({
                "ok": False,
                "error": "Committed locally, but git push failed — you can retry with `git push` yourself from a terminal.",
                "log": "\n\n".join(log)
            }), 500

        return jsonify({"ok": True, "log": "\n\n".join(log)})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?", "log": "\n\n".join(log)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}", "log": "\n\n".join(log)})


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>IEEE FSM — Local Content Admin</title>
<style>
  :root {
    --navy:#0D1117; --bg:#F4F8FC; --surface:#fff; --surface2:#EEF5FB;
    --line:rgba(17,32,51,.10); --text:#0D1B26; --muted:#5C7186;
    --blue:#00629B; --radius:12px; --mono:'Consolas','SF Mono',monospace;
    --danger:#C0392B; --danger-bg:#FCEBE9;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; }
  #app { display:flex; min-height:100vh; }
  nav { width:210px; flex-shrink:0; background:var(--navy); color:#E6EDF3; padding:24px 14px; }
  nav .brand { font-weight:700; margin-bottom:20px; padding:0 8px; }
  nav .brand span { display:block; font-weight:400; font-size:11px; color:#7D94A8; font-family:var(--mono); }
  nav button { display:block; width:100%; text-align:left; background:none; border:none; color:#B9C6D2;
    padding:10px 12px; border-radius:8px; font-size:14px; cursor:pointer; margin-bottom:4px; }
  nav button.active { background:var(--blue); color:#fff; }
  main { flex:1; padding:32px 40px; max-width:1200px; }
  main h2 { margin:0 0 4px; } main .sub { color:var(--muted); font-size:13.5px; margin:0 0 24px; }
  .panel { display:none; } .panel.active { display:block; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:24px; align-items:start; }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:20px 22px; }
  .card h3 { margin:0 0 14px; font-size:13.5px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-family:var(--mono); }
  label { display:block; font-size:13px; font-weight:600; margin:12px 0 5px; }
  label:first-of-type { margin-top:0; }
  input[type=text],input[type=url],input[type=date],textarea,select { width:100%; padding:9px 11px; border-radius:8px; border:1.5px solid var(--line); font-size:13.5px; background:var(--surface2); }
  textarea { resize:vertical; min-height:60px; font-family:inherit; }
  input[type=color] { width:48px; height:34px; border:1.5px solid var(--line); border-radius:8px; padding:2px; }
  .row { display:flex; gap:10px; } .row > div { flex:1; }
  .btn { padding:9px 16px; border-radius:999px; border:none; font-weight:600; font-size:13px; cursor:pointer; }
  .btn-p { background:var(--blue); color:#fff; } .btn-g { background:var(--surface2); border:1px solid var(--line); }
  .btnrow { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; align-items:center; }
  .entry { background:var(--surface2); border:1px solid var(--line); border-radius:9px; padding:10px 12px;
    display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  .entry b { display:block; font-size:13.5px; } .entry span { color:var(--muted); font-size:12px; }
  .entry .acts button { border:none; background:#fff; width:26px; height:26px; border-radius:6px; cursor:pointer; margin-left:4px; }
  .empty { color:var(--muted); font-size:13px; padding:16px; text-align:center; border:1.5px dashed var(--line); border-radius:9px; }
  .toast { position:fixed; bottom:22px; right:22px; background:var(--navy); color:#fff; padding:11px 18px;
    border-radius:9px; font-size:13px; opacity:0; transform:translateY(8px); transition:.25s; pointer-events:none; max-width:420px; z-index:50; }
  .toast.show { opacity:1; transform:translateY(0); }
  .toast.err { background:var(--danger); }
  .note { font-size:12px; color:var(--muted); background:var(--surface2); border-radius:7px; padding:9px 11px; margin-top:8px; line-height:1.5; }
  .entry-scroll { max-height:480px; overflow-y:auto; padding-right:6px; }
  .savebar { position:sticky; bottom:0; padding:14px 0; margin-top:24px; }
  .badge { display:inline-block; background:var(--danger-bg); color:var(--danger); font-size:11.5px; font-weight:700;
    padding:2px 8px; border-radius:999px; margin-left:8px; }
</style>
</head>
<body>
<div id="app">
  <nav>
    <div class="brand">IEEE FSM<span>local admin</span></div>
    <button class="tabbtn active" data-tab="activities">Activities</button>
    <button class="tabbtn" data-tab="units">Units &amp; Chapters</button>
    <button class="tabbtn" data-tab="stats">Hero Stats</button>
    <button class="tabbtn" data-tab="sections">Any Section</button>
    <button class="tabbtn" data-tab="fullfile">Full File</button>
    <button class="tabbtn" data-tab="publish">Publish</button>
  </nav>
  <main>
    <section class="panel active" id="p-activities">
      <h2>Activities &amp; events</h2>
      <p class="sub">Loaded directly from your index.html. Add, edit, or reorder, then save at the bottom.</p>
      <div class="grid2">
        <div class="card">
          <h3>Add event</h3>
          <label>Title</label><input type="text" id="aTitle">
          <label>Date</label><input type="date" id="aDate">
          <label>Description</label><textarea id="aDesc"></textarea>
          <label>Instagram/Facebook link</label><input type="url" id="aLink">
          <label>Photo</label><input type="file" id="aFile" accept="image/*">
          <div class="note">Uploads straight into your <code>Act_Images/</code> folder when you click Add.</div>
          <div class="btnrow"><button class="btn btn-p" id="aAddBtn" onclick="addActivity()">Add event</button>
          <button class="btn btn-g" id="aCancelBtn" style="display:none;" onclick="cancelActivityEdit()">Cancel edit</button></div>
        </div>
        <div class="card">
          <h3>Current events (<span id="aCount">0</span>)</h3>
          <div class="entry-scroll" id="aList"></div>
        </div>
      </div>
    </section>

    <section class="panel" id="p-units">
      <h2>Units &amp; chapters</h2>
      <p class="sub">Loaded directly from your index.html.</p>
      <div class="grid2">
        <div class="card">
          <h3>Add unit</h3>
          <label>Name</label><input type="text" id="uName">
          <div class="row"><div><label>Tag</label><input type="text" id="uTag"></div>
          <div><label>Color</label><input type="color" id="uColor" value="#00629B"></div></div>
          <label>Summary</label><textarea id="uSummary"></textarea>
          <label>Full details</label><textarea id="uDetails"></textarea>
          <label>Website/Instagram link</label><input type="url" id="uWebsite">
          <label>Logo</label><input type="file" id="uFile" accept="image/*">
          <div class="note">Uploads straight into your <code>Unit_Images/</code> folder when you click Add.</div>
          <div class="btnrow"><button class="btn btn-p" id="uAddBtn" onclick="addUnit()">Add unit</button>
          <button class="btn btn-g" id="uCancelBtn" style="display:none;" onclick="cancelUnitEdit()">Cancel edit</button></div>
        </div>
        <div class="card">
          <h3>Current units (<span id="uCount">0</span>)</h3>
          <div class="entry-scroll" id="uList"></div>
        </div>
      </div>
    </section>

    <section class="panel" id="p-stats">
      <h2>Hero stats</h2>
      <p class="sub">Loaded directly from your index.html. You can't add or remove rows here — the count has to match what's on the page, so only the label/value text is editable.</p>
      <div class="card" style="max-width:520px;">
        <h3>Numbers</h3>
        <div id="sForm"></div>
      </div>
    </section>

    <section class="panel" id="p-sections">
      <h2>Edit any section</h2>
      <p class="sub">Pick any &lt;section id="..."&gt; block on the page and edit its raw HTML directly — headings, text, links, images, anything inside it.</p>
      <div class="row" style="align-items:flex-end; margin-bottom:16px;">
        <div>
          <label>Section</label>
          <select id="sectionPicker" onchange="loadSection()"></select>
        </div>
        <div style="flex:0;">
          <button class="btn btn-g" onclick="refreshSections()" style="margin-top:0;">Refresh list</button>
        </div>
      </div>
      <div class="card">
        <h3>Raw HTML for this section</h3>
        <textarea id="sectionEditor" style="font-family:monospace; font-size:12.5px; white-space:pre; overflow:hidden;" oninput="autoGrow(this)"></textarea>
        <div class="btnrow">
          <button class="btn btn-p" onclick="saveSection()">Save this section</button>
          <span id="sectionStatus" style="font-size:13px; color:var(--muted);"></span>
        </div>
        <div class="note">This replaces everything between the section's opening and closing tags. A timestamped backup of the whole file is made first, and the save is validated (tag balance) before anything is written.</div>
      </div>
    </section>

    <section class="panel" id="p-fullfile">
      <h2>Full file editor</h2>
      <p class="sub">The entire index.html, raw. Use this for anything outside a &lt;section&gt; — nav, footer, head/meta, styles, scripts.</p>
      <div class="card">
        <div class="btnrow" style="margin-top:0;">
          <button class="btn btn-g" onclick="loadFullFile()">Load current file</button>
        </div>
        <textarea id="fullFileEditor" style="font-family:monospace; font-size:12px; white-space:pre; margin-top:12px; overflow:hidden;" oninput="autoGrow(this)"></textarea>
        <div class="btnrow">
          <button class="btn btn-p" onclick="saveFullFile()">Save entire file</button>
          <span id="fullFileStatus" style="font-size:13px; color:var(--muted);"></span>
        </div>
        <div class="note">This overwrites the whole file with exactly what's in the box below. A backup is made and the result is validated (tag balance, required containers still present) before anything is written — if validation fails, your file is left untouched. This is still the "edit literally anything" option, so double-check it regardless.</div>
      </div>
    </section>

    <section class="panel" id="p-publish">
      <h2>Publish</h2>
      <p class="sub">Runs git add / commit / push for index.html and your image folders — nothing else in the repo. Nothing here happens automatically; you always write the commit message and click the button.</p>
      <div class="card" style="max-width:640px;">
        <h3>Changed files</h3>
        <div id="gitStatusBox" class="note" style="margin-top:0;">Checking...</div>
        <div class="btnrow" style="margin-top:10px;">
          <button class="btn btn-g" onclick="checkGitStatus()">Refresh status</button>
        </div>

        <label style="margin-top:20px;">Commit message</label>
        <textarea id="commitMsg" placeholder="" style="min-height:70px;"></textarea>

        <div class="btnrow">
          <button class="btn btn-p" onclick="commitAndPush()">Commit &amp; push</button>
          <span id="pushStatus" style="font-size:13px; color:var(--muted);"></span>
        </div>
        <div class="note">Only <code>index.html</code>, <code>Act_Images/</code>, and <code>Unit_Images/</code> are staged — never a blanket "add everything," so nothing else in your repo gets swept in.</div>

        <div id="gitLog" style="display:none;">
          <label style="margin-top:16px;">Command output</label>
          <textarea readonly style="min-height:160px; font-family:var(--mono); font-size:12px; background:#0D1117; color:#9FE3A0; white-space:pre; overflow:auto;" id="gitLogText"></textarea>
        </div>
      </div>
    </section>

    <div class="savebar">
      <button class="btn btn-p" onclick="saveAll()">Save changes to index.html</button>
      <span id="saveStatus" style="margin-left:12px; font-size:13px; color:var(--muted);"></span>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>

<script>
let state = { activities: [], units: [], stats: [] };
let originalStatsCount = 0;

document.querySelectorAll('.tabbtn').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.getElementById('p-'+b.dataset.tab).classList.add('active');
  if(b.dataset.tab === 'sections') refreshSections();
  if(b.dataset.tab === 'fullfile') loadFullFile();
  if(b.dataset.tab === 'publish') checkGitStatus();
}));

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toast(m, isErr){ const t=document.getElementById('toast'); t.textContent=m; t.classList.toggle('err', !!isErr); t.classList.add('show'); setTimeout(()=>t.classList.remove('show'), isErr ? 4000 : 1800); }
function autoGrow(el){ el.style.height = 'auto'; el.style.height = (el.scrollHeight + 4) + 'px'; }

const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
function formatDateDisplay(iso){
  if(!iso) return '';
  const [y,m,d] = iso.split('-');
  return `${d} ${MONTHS[parseInt(m,10)-1]} ${y}`;
}
function parseDisplayDateToISO(str){
  const m = (str||'').match(/(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})/);
  if(!m) return '';
  const monthIdx = MONTHS.findIndex(mo => mo.toLowerCase() === m[2].toLowerCase());
  if(monthIdx === -1) return '';
  return `${m[3]}-${String(monthIdx+1).padStart(2,'0')}-${m[1].padStart(2,'0')}`;
}

async function safeFetch(url, opts){
  try {
    const r = await fetch(url, opts);
    let j;
    try { j = await r.json(); }
    catch(e) { throw new Error('Server did not return valid JSON (HTTP ' + r.status + ')'); }
    if(!r.ok && j.ok === undefined) j.ok = false;
    return j;
  } catch(e) {
    return { ok:false, error: 'Could not reach the local server — is admin_server.py still running? (' + e.message + ')' };
  }
}

async function loadState(){
  const j = await safeFetch('/api/state');
  if(j.ok === false){ toast('Could not load current content: ' + j.error, true); return; }
  state = j;
  originalStatsCount = (state.stats || []).length;
  renderActivities(); renderUnits(); renderStats();
}

async function uploadFile(inputEl, dir){
  if(!inputEl.files || !inputEl.files[0]) return null;
  const fd = new FormData();
  fd.append('file', inputEl.files[0]);
  fd.append('dir', dir);
  const j = await safeFetch('/api/upload', {method:'POST', body: fd});
  if(!j.ok){ toast('Upload failed: '+j.error, true); return null; }
  return j.filename;
}

/* ---- activities ---- */
let editingActivity = null; // index currently being edited, or null

async function addActivity(){
  const title = document.getElementById('aTitle').value.trim();
  if(!title){ toast('Add a title first.'); return; }
  const iso = document.getElementById('aDate').value;
  if(!iso){ toast('Pick a date first.'); return; }
  const fileInput = document.getElementById('aFile');
  let filename = editingActivity !== null ? state.activities[editingActivity].img : 'placeholder.jpg';
  if(fileInput.files && fileInput.files[0]){
    const uploaded = await uploadFile(fileInput, 'Act_Images');
    if(uploaded) filename = uploaded;
  }
  const entry = {
    title,
    date: formatDateDisplay(iso),
    desc: document.getElementById('aDesc').value.trim(),
    link: document.getElementById('aLink').value.trim(),
    img: filename
  };
  if(editingActivity !== null){
    state.activities[editingActivity] = entry;
    toast('Event updated (remember to Save)');
  } else {
    state.activities.push(entry);
    toast('Event added (remember to Save)');
  }
  cancelActivityEdit();
  renderActivities();
}
function editActivity(i){
  const a = state.activities[i];
  document.getElementById('aTitle').value = a.title;
  document.getElementById('aDate').value = parseDisplayDateToISO(a.date);
  document.getElementById('aDesc').value = a.desc;
  document.getElementById('aLink').value = a.link;
  document.getElementById('aFile').value = '';
  editingActivity = i;
  document.getElementById('aAddBtn').textContent = 'Save changes';
  document.getElementById('aCancelBtn').style.display = 'inline-block';
}
function cancelActivityEdit(){
  editingActivity = null;
  ['aTitle','aDate','aDesc','aLink'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('aFile').value = '';
  document.getElementById('aAddBtn').textContent = 'Add event';
  document.getElementById('aCancelBtn').style.display = 'none';
}
function delA(i){
  if(!confirm('Remove "' + state.activities[i].title + '" from the list? (Nothing is saved until you hit Save.)')) return;
  state.activities.splice(i,1);
  if(editingActivity===i) cancelActivityEdit();
  renderActivities();
}

let dragIndex = null;
function renderActivities(){
  document.getElementById('aCount').textContent = state.activities.length;
  const el = document.getElementById('aList');
  el.innerHTML = state.activities.length ? state.activities.map((a,i)=>`
    <div class="entry" draggable="true" data-i="${i}"
      ondragstart="dragIndex=${i}; this.style.opacity='0.4';"
      ondragend="this.style.opacity='1';"
      ondragover="event.preventDefault();"
      ondrop="dropActivity(${i});">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="cursor:grab; color:var(--muted);">&#9776;</span>
        <div><b>${esc(a.title)}</b><span>${esc(a.date)}</span></div>
      </div>
      <div class="acts"><button onclick="editActivity(${i})" title="Edit">&#9998;</button><button onclick="delA(${i})" title="Delete">&times;</button></div></div>`).join('')
    : '<div class="empty">No events.</div>';
}
function dropActivity(dropIdx){
  if(dragIndex === null || dragIndex === dropIdx) return;
  const [moved] = state.activities.splice(dragIndex, 1);
  state.activities.splice(dropIdx, 0, moved);
  dragIndex = null;
  renderActivities();
}

/* ---- units ---- */
let editingUnit = null;

async function addUnit(){
  const name = document.getElementById('uName').value.trim();
  if(!name){ toast('Add a name first.'); return; }
  const fileInput = document.getElementById('uFile');
  let filename = editingUnit !== null ? state.units[editingUnit].logo : 'placeholder-logo.svg';
  if(fileInput.files && fileInput.files[0]){
    const uploaded = await uploadFile(fileInput, 'Unit_Images');
    if(uploaded) filename = uploaded;
  }
  const entry = {
    name, tag: document.getElementById('uTag').value.trim(),
    color: document.getElementById('uColor').value,
    summary: document.getElementById('uSummary').value.trim(),
    details: document.getElementById('uDetails').value.trim(),
    website: document.getElementById('uWebsite').value.trim(),
    logo: filename,
    logo_id: editingUnit !== null ? (state.units[editingUnit].logo_id || '') : ''
  };
  if(editingUnit !== null){
    state.units[editingUnit] = entry;
    toast('Unit updated (remember to Save)');
  } else {
    state.units.push(entry);
    toast('Unit added (remember to Save)');
  }
  cancelUnitEdit();
  renderUnits();
}
function editUnit(i){
  const u = state.units[i];
  document.getElementById('uName').value = u.name;
  document.getElementById('uTag').value = u.tag;
  document.getElementById('uColor').value = u.color || '#00629B';
  document.getElementById('uSummary').value = u.summary;
  document.getElementById('uDetails').value = u.details;
  document.getElementById('uWebsite').value = u.website;
  document.getElementById('uFile').value = '';
  editingUnit = i;
  document.getElementById('uAddBtn').textContent = 'Save changes';
  document.getElementById('uCancelBtn').style.display = 'inline-block';
}
function cancelUnitEdit(){
  editingUnit = null;
  ['uName','uTag','uSummary','uDetails','uWebsite'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('uColor').value = '#00629B';
  document.getElementById('uFile').value = '';
  document.getElementById('uAddBtn').textContent = 'Add unit';
  document.getElementById('uCancelBtn').style.display = 'none';
}
function delU(i){
  if(!confirm('Remove "' + state.units[i].name + '" from the list? (Nothing is saved until you hit Save.)')) return;
  state.units.splice(i,1);
  if(editingUnit===i) cancelUnitEdit();
  renderUnits();
}

let dragUnitIndex = null;
function renderUnits(){
  document.getElementById('uCount').textContent = state.units.length;
  const el = document.getElementById('uList');
  el.innerHTML = state.units.length ? state.units.map((u,i)=>`
    <div class="entry" draggable="true"
      ondragstart="dragUnitIndex=${i}; this.style.opacity='0.4';"
      ondragend="this.style.opacity='1';"
      ondragover="event.preventDefault();"
      ondrop="dropUnit(${i});">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="cursor:grab; color:var(--muted);">&#9776;</span>
        <div><b>${esc(u.name)}</b><span>${esc(u.tag)}</span></div>
      </div>
      <div class="acts"><button onclick="editUnit(${i})" title="Edit">&#9998;</button><button onclick="delU(${i})" title="Delete">&times;</button></div></div>`).join('')
    : '<div class="empty">No units.</div>';
}
function dropUnit(dropIdx){
  if(dragUnitIndex === null || dragUnitIndex === dropIdx) return;
  const [moved] = state.units.splice(dragUnitIndex, 1);
  state.units.splice(dropIdx, 0, moved);
  dragUnitIndex = null;
  renderUnits();
}

/* ---- stats ---- */
function renderStats(){
  document.getElementById('sForm').innerHTML = state.stats.map((s,i)=>`
    <div class="row" style="margin-bottom:10px;">
      <div><label style="margin-top:${i===0?'0':'12px'};">Label</label><input type="text" value="${esc(s.label)}" onchange="state.stats[${i}].label=this.value"></div>
      <div><label style="margin-top:${i===0?'0':'12px'};">Value</label><input type="text" value="${esc(s.value)}" onchange="state.stats[${i}].value=this.value"></div>
    </div>`).join('');
}

/* ---- save ---- */
async function saveAll(){
  const summary = `${state.activities.length} activities, ${state.units.length} units, ${state.stats.length} stats`;
  if(!confirm('Save to index.html now?\\n\\n' + summary + '\\n\\nA timestamped backup will be made first.')) return;
  document.getElementById('saveStatus').textContent = 'Saving...';
  const j = await safeFetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(state)});
  if(j.ok){
    document.getElementById('saveStatus').textContent = 'Saved to ' + j.path + ' (a timestamped .bak file was also created)';
    toast('index.html updated');
    loadState();
  } else {
    document.getElementById('saveStatus').textContent = '';
    toast('Not saved — ' + j.error, true);
  }
}

/* ---- any section ---- */
async function refreshSections(){
  const list = await safeFetch('/api/sections');
  if(list.ok === false){ toast('Could not load section list: ' + list.error, true); return; }
  const sel = document.getElementById('sectionPicker');
  sel.innerHTML = list.map(s => `<option value="${esc(s.id)}">${esc(s.label)} (#${esc(s.id)})</option>`).join('');
  if(list.length) loadSection();
}
async function loadSection(){
  const id = document.getElementById('sectionPicker').value;
  if(!id) return;
  const j = await safeFetch('/api/section/' + encodeURIComponent(id));
  const el = document.getElementById('sectionEditor');
  el.value = j.ok ? j.html : ('Error: ' + j.error);
  autoGrow(el);
}
async function saveSection(){
  const id = document.getElementById('sectionPicker').value;
  if(!id) return;
  if(!confirm('Save changes to section "' + id + '"? A backup will be made first.')) return;
  const htmlVal = document.getElementById('sectionEditor').value;
  const j = await safeFetch('/api/section/' + encodeURIComponent(id), {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({html: htmlVal})
  });
  document.getElementById('sectionStatus').textContent = j.ok ? ('Saved (backup created)') : '';
  toast(j.ok ? 'Section #' + id + ' saved to index.html' : 'Not saved — ' + j.error, !j.ok);
}

/* ---- full file ---- */
async function loadFullFile(){
  const j = await safeFetch('/api/fullfile');
  const el = document.getElementById('fullFileEditor');
  el.value = j.ok ? j.html : ('Error: ' + j.error);
  autoGrow(el);
}
async function saveFullFile(){
  if(!confirm('This overwrites the entire index.html with what is in the box. A backup will be made, and the result is checked for balanced tags before writing, but double check you have not broken anything logically. Continue?')) return;
  const htmlVal = document.getElementById('fullFileEditor').value;
  const j = await safeFetch('/api/fullfile', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({html: htmlVal})
  });
  document.getElementById('fullFileStatus').textContent = j.ok ? 'Saved (backup created)' : '';
  toast(j.ok ? 'Whole file saved' : 'Not saved — ' + j.error, !j.ok);
}

/* ---- publish (git) ---- */
async function checkGitStatus(){
  const box = document.getElementById('gitStatusBox');
  box.textContent = 'Checking...';
  const j = await safeFetch('/api/git/status');
  if(j.ok === false){ box.textContent = 'Could not check status: ' + j.error; return; }
  if(j.clean){
    box.textContent = 'No changes in index.html, Act_Images/, or Unit_Images/ compared to the last commit.';
  } else {
    box.innerHTML = '<b>' + j.changed.length + ' changed file(s):</b><br>' + j.changed.map(esc).join('<br>');
  }
}
async function commitAndPush(){
  const message = document.getElementById('commitMsg').value.trim();
  if(!message){ toast('Write a commit message first.'); return; }
  if(!confirm('Commit and push now with message:\\n\\n"' + message + '"\\n\\nThis will run git add, commit, and push for real.')) return;
  document.getElementById('pushStatus').textContent = 'Working...';
  const j = await safeFetch('/api/git/push', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message})});
  if(j.log){
    document.getElementById('gitLog').style.display = 'block';
    document.getElementById('gitLogText').value = j.log;
  }
  document.getElementById('pushStatus').textContent = j.ok ? 'Pushed successfully.' : '';
  toast(j.ok ? 'Committed and pushed' : 'Not pushed — ' + j.error, !j.ok);
  if(j.ok){
    document.getElementById('commitMsg').value = '';
    checkGitStatus();
  }
}

loadState();
refreshSections();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"\n  IEEE FSM local admin running at http://127.0.0.1:{PORT}")
    print(f"  Editing: {os.path.abspath(INDEX_HTML_PATH)}\n")
    app.run(port=PORT, debug=False)