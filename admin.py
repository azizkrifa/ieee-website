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
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename

# ============================================================
# CONFIG — edit these to match your repo layout
# ============================================================
INDEX_HTML_PATH = "index.html"       # path to your site's index.html
ACTIVITIES_IMG_DIR = "Act_Images"    # folder used by <img src="Act_Images/...">
UNITS_IMG_DIR = "Unit_Images"        # folder for unit logos (adjust if yours differs)
PORT = 5055

app = Flask(__name__)


# ============================================================
# Low-level helpers: find a container's inner HTML by class,
# using simple tag-depth counting (no HTML parser, so nothing
# else in the file is ever touched or re-serialized).
# ============================================================
def find_container_span(html, class_name, tag="div"):
    """Return (inner_start, inner_end, outer_end) for the first
    <tag class="...class_name..."> ... </tag> block, matching nested
    tags of the same name by depth."""
    open_re = re.compile(r'<' + tag + r'\b[^>]*>')
    close_tag = '</' + tag + '>'

    m = re.search(r'<' + tag + r'\b[^>]*class="[^"]*\b' + re.escape(class_name) + r'\b[^"]*"[^>]*>', html)
    if not m:
        return None
    inner_start = m.end()
    depth = 1
    pos = inner_start
    while depth > 0:
        next_open = open_re.search(html, pos)
        next_close = html.find(close_tag, pos)
        if next_close == -1:
            raise ValueError(f"Could not find matching close for <{tag} class=\"{class_name}\">")
        if next_open and next_open.start() < next_close:
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close + len(close_tag)
    inner_end = pos - len(close_tag)
    return inner_start, inner_end, pos


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
    return html_lib.unescape(val) if unescape else val


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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
        year_m = re.search(r'data-year="(\d{4})"', block)
        items.append({
            "year": year_m.group(1) if year_m else "2026",
            "date": text_of(r'<div class="tl-date">(.*?)</div>', block),
            "title": text_of(r'<h3>(.*?)</h3>', block),
            "desc": text_of(r'<h3>.*?</h3>\s*<p>(.*?)</p>', block),
            "link": text_of(r'<a class="tl-more" href="(.*?)"', block),
            "img": text_of(r'<img src="(?:' + ACTIVITIES_IMG_DIR + r'/)?(.*?)"', block, "placeholder.jpg"),
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
        items.append({
            "name": text_of(r'<h3>(.*?)</h3>', block),
            "tag": text_of(r'<span class="tag">(.*?)</span>', block),
            "color": color_m.group(1).strip() if color_m else "#00629B",
            "summary": text_of(r'<h3>.*?</h3>\s*<p>(.*?)</p>', block),
            "details": text_of(r'data-details="(.*?)"', block),
            "website": text_of(r'data-website="(.*?)"', block),
            "logo": text_of(r'<img src="(.*?)"', block, "placeholder-logo.svg"),
        })
    return items


def parse_stats(html):
    rows = re.findall(
        r'<div class="hero-stat-row"><span class="label">(.*?)</span><span class="num">(.*?)</span></div>',
        html
    )
    return [{"label": html_lib.unescape(l.strip()), "value": html_lib.unescape(v.strip())} for l, v in rows]


# ============================================================
# Serialization: build HTML blocks from state
# ============================================================
def build_activity_block(a):
    return f'''          <div class="tl-item" data-year="{esc(a['year'])}">
            <div class="tl-date">{esc(a['date'])}</div>
            <div class="tl-node-col">
              <div class="tl-node"></div>
            </div>
            <div class="tl-card">
              <div>
                <h3>{esc(a['title'])}</h3>
                <p>{esc(a['desc'])}</p>
                <a class="tl-more" href="{esc(a['link'])}" target="_blank" rel="noopener">
                  See more <span class="tl-more-arrow">&rarr;</span>
                </a>
              </div>
              <div class="tl-thumb">
                <img src="{ACTIVITIES_IMG_DIR}/{esc(a['img'])}" alt="{esc(a['title'])}"
                  onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="ph-icon" style="display:none;"><svg class="icon" style="width:30px;height:30px;">
                    <use href="#i-camera" />
                  </svg></div>
              </div>
            </div>
          </div>'''


def build_unit_block(u):
    return f'''          <div class="unit-card reveal" style="--unit-color:{esc(u['color'])};"
               data-website="{esc(u['website'])}"
               data-details="{esc(u['details'])}">
            <div class="unit-logo-img"><img src="{esc(u['logo'])}" alt="{esc(u['name'])} Logo"
                style="width: 180px; height: auto; padding: 25px;"
                onerror="this.style.display='none';"></div>
            <span class="tag">{esc(u['tag'])}</span>
            <h3>{esc(u['name'])}</h3>
            <p>{esc(u['summary'])}</p>
          </div>'''


def replace_container(html, class_name, new_inner_html):
    span = find_container_span(html, class_name)
    if not span:
        raise ValueError(f"Could not find container with class {class_name}")
    inner_start, inner_end, _ = span
    return html[:inner_start] + "\n" + new_inner_html + "\n        " + html[inner_end:]


def replace_stats(html, stats):
    it = iter(stats)
    def repl(_m):
        s = next(it)
        return f'<div class="hero-stat-row"><span class="label">{esc(s["label"])}</span><span class="num">{esc(s["value"])}</span></div>'
    new_html, n = re.subn(
        r'<div class="hero-stat-row"><span class="label">.*?</span><span class="num">.*?</span></div>',
        repl, html
    )
    return new_html


def backup_index():
    if os.path.exists(INDEX_HTML_PATH):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(INDEX_HTML_PATH, f"{INDEX_HTML_PATH}.bak_{ts}")


# ============================================================
# Routes
# ============================================================
@app.route("/api/state")
def api_state():
    html = read_index_html()
    return jsonify({
        "activities": parse_activities(html),
        "units": parse_units(html),
        "stats": parse_stats(html),
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    file = request.files.get("file")
    target_dir = request.form.get("dir", ACTIVITIES_IMG_DIR)
    if not file or file.filename == "":
        return jsonify({"ok": False, "error": "No file provided"}), 400
    filename = secure_filename(file.filename)
    os.makedirs(target_dir, exist_ok=True)
    save_path = os.path.join(target_dir, filename)
    file.save(save_path)
    return jsonify({"ok": True, "filename": filename, "path": save_path})


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json()
    html = read_index_html()
    backup_index()

    activities_html = "\n\n".join(build_activity_block(a) for a in data.get("activities", []))
    units_html = "\n\n".join(build_unit_block(u) for u in data.get("units", []))

    html = replace_container(html, "timeline", activities_html)
    html = replace_container(html, "units-grid", units_html)
    html = replace_stats(html, data.get("stats", []))

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    return jsonify({"ok": True, "path": os.path.abspath(INDEX_HTML_PATH)})


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
  input[type=text],input[type=url],textarea,select { width:100%; padding:9px 11px; border-radius:8px; border:1.5px solid var(--line); font-size:13.5px; background:var(--surface2); }
  textarea { resize:vertical; min-height:60px; font-family:inherit; }
  input[type=color] { width:48px; height:34px; border:1.5px solid var(--line); border-radius:8px; padding:2px; }
  .row { display:flex; gap:10px; } .row > div { flex:1; }
  .btn { padding:9px 16px; border-radius:999px; border:none; font-weight:600; font-size:13px; cursor:pointer; }
  .btn-p { background:var(--blue); color:#fff; } .btn-g { background:var(--surface2); border:1px solid var(--line); }
  .btnrow { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }
  .entry { background:var(--surface2); border:1px solid var(--line); border-radius:9px; padding:10px 12px;
    display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  .entry b { display:block; font-size:13.5px; } .entry span { color:var(--muted); font-size:12px; }
  .entry .acts button { border:none; background:#fff; width:26px; height:26px; border-radius:6px; cursor:pointer; margin-left:4px; }
  .empty { color:var(--muted); font-size:13px; padding:16px; text-align:center; border:1.5px dashed var(--line); border-radius:9px; }
  .toast { position:fixed; bottom:22px; right:22px; background:var(--navy); color:#fff; padding:11px 18px;
    border-radius:9px; font-size:13px; opacity:0; transform:translateY(8px); transition:.25s; pointer-events:none; }
  .toast.show { opacity:1; transform:translateY(0); }
  .note { font-size:12px; color:var(--muted); background:var(--surface2); border-radius:7px; padding:9px 11px; margin-top:8px; line-height:1.5; }
  .savebar { position:sticky; bottom:0; background:var(--surface); border-top:1px solid var(--line); padding:14px 0; margin-top:24px; }
</style>
</head>
<body>
<div id="app">
  <nav>
    <div class="brand">IEEE FSM<span>local admin</span></div>
    <button class="tabbtn active" data-tab="activities">Activities</button>
    <button class="tabbtn" data-tab="units">Units &amp; Chapters</button>
    <button class="tabbtn" data-tab="stats">Hero Stats</button>
  </nav>
  <main>
    <section class="panel active" id="p-activities">
      <h2>Activities &amp; events</h2>
      <p class="sub">Loaded directly from your index.html. Add, edit, or reorder, then save at the bottom.</p>
      <div class="grid2">
        <div class="card">
          <h3>Add event</h3>
          <label>Title</label><input type="text" id="aTitle">
          <div class="row"><div><label>Date</label><input type="text" id="aDate" placeholder="e.g. 08 April 2026"></div>
          <div><label>Year</label><select id="aYear"><option value="2025">2025</option><option value="2026" selected>2026</option></select></div></div>
          <label>Description</label><textarea id="aDesc"></textarea>
          <label>Instagram/Facebook link</label><input type="url" id="aLink">
          <label>Photo</label><input type="file" id="aFile" accept="image/*">
          <div class="note">Uploads straight into your <code>Act_Images/</code> folder when you click Add.</div>
          <div class="btnrow"><button class="btn btn-p" onclick="addActivity()">Add event</button></div>
        </div>
        <div class="card">
          <h3>Current events (<span id="aCount">0</span>)</h3>
          <div id="aList"></div>
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
          <div class="btnrow"><button class="btn btn-p" onclick="addUnit()">Add unit</button></div>
        </div>
        <div class="card">
          <h3>Current units (<span id="uCount">0</span>)</h3>
          <div id="uList"></div>
        </div>
      </div>
    </section>

    <section class="panel" id="p-stats">
      <h2>Hero stats</h2>
      <p class="sub">Loaded directly from your index.html.</p>
      <div class="card" style="max-width:520px;">
        <h3>Numbers</h3>
        <div id="sForm"></div>
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

document.querySelectorAll('.tabbtn').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.getElementById('p-'+b.dataset.tab).classList.add('active');
}));

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),1800); }

async function loadState(){
  const r = await fetch('/api/state');
  state = await r.json();
  renderActivities(); renderUnits(); renderStats();
}

async function uploadFile(inputEl, dir){
  if(!inputEl.files || !inputEl.files[0]) return null;
  const fd = new FormData();
  fd.append('file', inputEl.files[0]);
  fd.append('dir', dir);
  const r = await fetch('/api/upload', {method:'POST', body: fd});
  const j = await r.json();
  if(!j.ok){ toast('Upload failed: '+j.error); return null; }
  return j.filename;
}

/* ---- activities ---- */
async function addActivity(){
  const title = document.getElementById('aTitle').value.trim();
  if(!title){ toast('Add a title first.'); return; }
  const filename = await uploadFile(document.getElementById('aFile'), 'Act_Images') || 'placeholder.jpg';
  state.activities.push({
    title, date: document.getElementById('aDate').value.trim(),
    year: document.getElementById('aYear').value,
    desc: document.getElementById('aDesc').value.trim(),
    link: document.getElementById('aLink').value.trim(),
    img: filename
  });
  ['aTitle','aDate','aDesc','aLink'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('aFile').value = '';
  renderActivities();
  toast('Event added (remember to Save)');
}
function moveA(i,d){ const j=i+d; if(j<0||j>=state.activities.length) return; [state.activities[i],state.activities[j]]=[state.activities[j],state.activities[i]]; renderActivities(); }
function delA(i){ state.activities.splice(i,1); renderActivities(); }
function renderActivities(){
  document.getElementById('aCount').textContent = state.activities.length;
  const el = document.getElementById('aList');
  el.innerHTML = state.activities.length ? state.activities.map((a,i)=>`
    <div class="entry"><div><b>${esc(a.title)}</b><span>${esc(a.date)} · ${a.year}</span></div>
    <div class="acts"><button onclick="moveA(${i},-1)">↑</button><button onclick="moveA(${i},1)">↓</button><button onclick="delA(${i})">×</button></div></div>`).join('')
    : '<div class="empty">No events.</div>';
}

/* ---- units ---- */
async function addUnit(){
  const name = document.getElementById('uName').value.trim();
  if(!name){ toast('Add a name first.'); return; }
  const filename = await uploadFile(document.getElementById('uFile'), 'Unit_Images') || 'placeholder-logo.svg';
  state.units.push({
    name, tag: document.getElementById('uTag').value.trim(),
    color: document.getElementById('uColor').value,
    summary: document.getElementById('uSummary').value.trim(),
    details: document.getElementById('uDetails').value.trim(),
    website: document.getElementById('uWebsite').value.trim(),
    logo: filename
  });
  ['uName','uTag','uSummary','uDetails','uWebsite'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('uFile').value = '';
  renderUnits();
  toast('Unit added (remember to Save)');
}
function delU(i){ state.units.splice(i,1); renderUnits(); }
function renderUnits(){
  document.getElementById('uCount').textContent = state.units.length;
  const el = document.getElementById('uList');
  el.innerHTML = state.units.length ? state.units.map((u,i)=>`
    <div class="entry"><div><b>${esc(u.name)}</b><span>${esc(u.tag)}</span></div>
    <div class="acts"><button onclick="delU(${i})">×</button></div></div>`).join('')
    : '<div class="empty">No units.</div>';
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
  document.getElementById('saveStatus').textContent = 'Saving...';
  const r = await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(state)});
  const j = await r.json();
  if(j.ok){
    document.getElementById('saveStatus').textContent = 'Saved to ' + j.path + ' (a timestamped .bak file was also created)';
    toast('index.html updated');
  } else {
    document.getElementById('saveStatus').textContent = 'Error: ' + j.error;
  }
}

loadState();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"\n  IEEE FSM local admin running at http://127.0.0.1:{PORT}")
    print(f"  Editing: {os.path.abspath(INDEX_HTML_PATH)}\n")
    app.run(port=PORT, debug=False)
