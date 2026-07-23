#!/usr/bin/env python3
"""
IEEE FSM Student Branch — local content admin server.

Run this from inside your local repo (same folder as index.html), or point
INDEX_HTML_PATH below at it. It serves a small dashboard at
http://127.0.0.1:5500/admin that reads your real index.html, lets you add/edit/
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
from functools import wraps
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

# ============================================================
# CONFIG — edit these to match your repo layout
# ============================================================

load_dotenv()

INDEX_HTML_PATH = "index.html"       # path to your site's index.html
ACTIVITIES_IMG_DIR = "Act_Images"    # folder used by <img src="Act_Images/...">
UNITS_IMG_DIR = "Unit_Images"        # folder for unit logos (adjust if yours differs)
TEAM_IMG_DIR = "assets/team"         # folder used by <img src="assets/team/...">
PARTNERS_IMG_DIR = "Logos"           # folder used by <img src="Logos/...">
GALLERY_IMG_DIR = "gallery"          # folder used by <img src="gallery/...">
IMG_DIR = "Images"                   # folder for other images (not used by this tool, but git push includes it)
REPO_DIR = "."                       # folder containing your git repo — usually same folder as this script
DASHBOARD_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "admin_website_dashboard.html"
)                                     # the dashboard's own HTML/CSS/JS, kept in a separate file
PORT = int(os.getenv("PORT2"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
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


def js_str(s):
    """Escape a Python string for embedding inside a single-quoted JS string
    literal (used to rewrite the gallery `photos` array)."""
    return (s or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", "")


def sniff_web_image(header):
    """Return a short format name ('jpeg', 'png', ...) if `header` (the first
    bytes of a file) is a raster/vector image a browser can actually render,
    or None otherwise. This is a magic-number check on the real bytes, so a
    RAW/DNG/HEIC/TIFF file renamed to .png is still correctly rejected."""
    if header[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if header[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    # ISO-BMFF 'ftyp' box at offset 4 — used by AVIF (browser-viewable). HEIC
    # ('heic'/'heif' brands) uses the same container but browsers can't show it,
    # so only AVIF brands are accepted here.
    if header[4:8] == b'ftyp' and header[8:12] in (b'avif', b'avis'):
        return 'avif'
    head = header.lstrip()[:256].lower()
    if head[:5] == b'<?xml' or head[:4] == b'<svg':
        return 'svg'
    return None


def sniff_web_media(header):
    """Return (format, kind) where kind is 'image' or 'video', or (None, None)
    if the file is not a browser-renderable media type."""
    img = sniff_web_image(header)
    if img:
        return (img, 'image')
    # MP4 — ISO-BMFF ftyp box (not AVIF, already handled above)
    if header[4:8] == b'ftyp':
        return ('mp4', 'video')
    # WebM — starts with EBML header magic bytes
    if header[:4] == b'\x1a\x45\xdf\xa3':
        return ('webm', 'video')
    return (None, None)


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
    for required in ('<div class="timeline">', '<div class="units-grid">',
                      '<div class="team-grid">', '<div class="partners-grid">',
                      'const photos = [', "</html>"):
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


def href_by_aria_label(block, label, default="#"):
    """Find the href of the <a> tag whose aria-label matches `label`. Matches
    the whole opening tag first (bounded by '>' so it can never cross into a
    neighboring <a> tag), then pulls href out of just that tag — independent
    of attribute order, same approach as img_src_of above."""
    m = re.search(r'<a\b[^>]*\baria-label="' + re.escape(label) + r'"[^>]*>', block, re.S)
    if not m:
        return default
    hm = re.search(r'\bhref="(.*?)"', m.group(0))
    return html_lib.unescape(hm.group(1)) if hm else default


def parse_team(html):
    span = find_container_span(html, "team-grid")
    if not span:
        return []
    inner = html[span[0]:span[1]]
    items = []
    for start, end in top_level_blocks(inner, "team-card"):
        block = inner[start:end]
        items.append({
            "name": text_of(r'<div class="name">(.*?)</div>', block),
            "role": text_of(r'<div class="role">(.*?)</div>', block),
            "photo": img_src_of(block, f"{TEAM_IMG_DIR}/placeholder.jpg"),
            "initials": text_of(r'<div class="avatar-fallback"[^>]*>(.*?)</div>', block),
            "linkedin": href_by_aria_label(block, "LinkedIn"),
            "instagram": href_by_aria_label(block, "Instagram"),
            "facebook": href_by_aria_label(block, "Facebook"),
            "raw_html": block,
        })
    return items


def parse_partners(html):
    span = find_container_span(html, "partners-grid")
    if not span:
        return []
    inner = html[span[0]:span[1]]
    items = []
    # Partners are logo-only <div class="partner-tile"> (or legacy <a>) tiles.
    # The partner name lives in the title/aria-label/data-name attribute; the site is data-website or href.
    for start, end in top_level_blocks(inner, "partner-tile", tag="div"):
        block = inner[start:end]
        name = text_of(r'\bdata-name="(.*?)"', block) or text_of(r'\btitle="(.*?)"', block) or text_of(r'\baria-label="(.*?)"', block)
        items.append({
            "name": name,
            "website": text_of(r'\bdata-website="(.*?)"', block) or text_of(r'\bhref="(.*?)"', block),
            "description": text_of(r'\bdata-description="(.*?)"', block) or "",
            "logo": img_src_of(block, "placeholder-logo.svg"),
            "fallback": text_of(r'<span class="partner-fallback">(.*?)</span>', block),
            "raw_html": block,
        })
    # Also handle legacy <a> tags for backward compat during transition
    if not items:
        for start, end in top_level_blocks(inner, "partner-tile", tag="a"):
            block = inner[start:end]
            name = text_of(r'\btitle="(.*?)"', block) or text_of(r'\baria-label="(.*?)"', block)
            items.append({
                "name": name,
                "website": text_of(r'\bhref="(.*?)"', block),
                "logo": img_src_of(block, "placeholder-logo.svg"),
                "fallback": text_of(r'<span class="partner-fallback">(.*?)</span>', block),
                "raw_html": block,
            })
    return items


GALLERY_PHOTOS_RE = re.compile(r'(const photos = \[)(.*?)(\];)', re.S)


def find_gallery_photos_span(html):
    """Return the regex match for the gallery marquee's `const photos = [ ... ];`
    array, or None. The gallery is now JS-driven (an auto-scrolling marquee
    built from this array), not a static HTML container."""
    return GALLERY_PHOTOS_RE.search(html)


def _js_unstr(m):
    """Undo js_str escaping for a value captured from a JS string literal."""
    return m.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def parse_gallery(html):
    m = find_gallery_photos_span(html)
    if not m:
        return []
    body = m.group(2)
    items = []
    # each entry looks like: { src: 'gallery/x.jpg', alt: 'caption', type: 'video' }
    for entry in re.finditer(r'\{[^}]*\}', body):
        block = entry.group(0)
        # capture single- or double-quoted values, honoring backslash-escaped quotes
        src_m = re.search(r'src:\s*(?:\'((?:\\.|[^\'\\])*)\'|"((?:\\.|[^"\\])*)")', block)
        alt_m = re.search(r'alt:\s*(?:\'((?:\\.|[^\'\\])*)\'|"((?:\\.|[^"\\])*)")', block)
        type_m = re.search(r'type:\s*(?:\'((?:\\.|[^\'\\])*)\'|"((?:\\.|[^"\\])*)")', block)
        if not src_m:
            continue
        src = _js_unstr(src_m.group(1) if src_m.group(1) is not None else src_m.group(2))
        alt = ""
        if alt_m:
            alt = _js_unstr(alt_m.group(1) if alt_m.group(1) is not None else alt_m.group(2))
        media_type = ""
        if type_m:
            media_type = _js_unstr(type_m.group(1) if type_m.group(1) is not None else type_m.group(2))
        item = {
            "photo": src,
            "title": alt,
            "alt": alt,
        }
        if media_type:
            item["type"] = media_type
        items.append(item)
    return items


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


def build_team_block(t):
    return f'''          <div class="team-card">
            <div class="team-photo">
              <img src="{esc(t.get("photo", f"{TEAM_IMG_DIR}/placeholder.jpg"))}" alt="{esc(t.get("role", ""))}"
                onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
              <div class="avatar-fallback" style="display:none;">{esc(t.get("initials", ""))}</div>
            </div>
            <div class="team-overlay">
              <div class="team-socials">
                <a href="{esc(t.get("linkedin", "#"))}" target="_blank" aria-label="LinkedIn">
                  <i class="fab fa-linkedin-in"></i>
                </a>

                <a href="{esc(t.get("instagram", "#"))}" target="_blank" aria-label="Instagram">
                  <i class="fab fa-instagram"></i>
                </a>

                <a href="{esc(t.get("facebook", "#"))}" target="_blank" aria-label="Facebook">
                  <i class="fab fa-facebook-f"></i>
                </a>
              </div>
              <div class="name">{esc(t.get("name", ""))}</div>
              <div class="role">{esc(t.get("role", ""))}</div>
            </div>
          </div>'''


def build_partner_block(p):
    name = p.get("name", "")
    return f'''          <div class="partner-tile reveal" data-name="{esc(name)}" data-website="{esc(p.get("website", ""))}" data-description="{esc(p.get("description", ""))}"
            title="{esc(name)}" aria-label="{esc(name)}" role="button" tabindex="0">
            <img src="{esc(p.get("logo", "placeholder-logo.svg"))}" alt="{esc(name)} logo"
              onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
            <span class="partner-fallback">{esc(p.get("fallback", "") or name)}</span>
          </div>'''


def team_output_html(t):
    raw = t.get("raw_html")
    return raw if raw else build_team_block(t)


def partner_output_html(p):
    raw = p.get("raw_html")
    return raw if raw else build_partner_block(p)


def build_gallery_entry(g):
    """One line of the JS `photos` array. `alt` doubles as the caption/label."""
    alt = g.get("alt") or g.get("title", "")
    type_part = ""
    if g.get("type") == "video":
        type_part = ", type: 'video'"
    return f"        {{ src: '{js_str(g.get('photo', ''))}', alt: '{js_str(alt)}'{type_part} }},"


def replace_gallery_photos(html, gallery):
    """Rewrite the gallery marquee's `const photos = [ ... ];` array from state.
    The gallery is JS-driven now, so there is no HTML container to replace."""
    m = find_gallery_photos_span(html)
    if not m:
        raise ValidationError(
            "Could not find the gallery `const photos = [ ... ];` array — nothing was changed."
        )
    if gallery:
        lines = "\n".join(build_gallery_entry(g) for g in gallery)
        new_body = "\n" + lines + "\n      "
    else:
        new_body = "\n      "
    return html[:m.start()] + m.group(1) + new_body + m.group(3) + html[m.end():]


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

def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response(
        "Authentication required.",
        401,
        {
            "WWW-Authenticate": 'Basic realm="IEEE FSM Admin"'
        },
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# ============================================================
# Routes
# ============================================================
@app.route("/api/state")
@requires_auth
def api_state():
    try:
        html = read_index_html()
        return jsonify({
            "activities": parse_activities(html),
            "units": parse_units(html),
            "team": parse_team(html),
            "partners": parse_partners(html),
            "gallery": parse_gallery(html),
            "stats": parse_stats(html),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
@requires_auth
def api_upload():
    file = request.files.get("file")
    target_dir = request.form.get("dir", ACTIVITIES_IMG_DIR)
    if not file or file.filename == "":
        return jsonify({"ok": False, "error": "No file provided"}), 400
    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"ok": False, "error": "That filename isn't valid"}), 400
    # Sniff the actual bytes — the browser's accept="image/*" trusts the OS,
    # which happily reports iPhone/camera RAW files (DNG, HEIC, TIFF) as images
    # even though no browser can render them. Rejecting here means a clear
    # error in the panel instead of a broken tile that only shows a fallback.
    header = file.stream.read(64)
    file.stream.seek(0)
    fmt, kind = sniff_web_media(header)
    if not fmt:
        return jsonify({"ok": False, "error": (
            "That file isn't a web-viewable image or video. Browsers can display "
            "JPEG, PNG, GIF, WebP, AVIF, SVG images and MP4/WebM videos — this "
            "looks like an unsupported format. Export or convert it first."
        )}), 400
    # Only allow video uploads to the gallery directory
    if kind == 'video' and target_dir != GALLERY_IMG_DIR:
        return jsonify({"ok": False, "error": (
            "Videos can only be uploaded to the gallery."
        )}), 400
    os.makedirs(target_dir, exist_ok=True)
    save_path = os.path.join(target_dir, filename)
    file.save(save_path)
    return jsonify({"ok": True, "filename": filename, "path": save_path, "kind": kind})


@app.route("/api/save", methods=["POST"])
@requires_auth
def api_save():
    try:
        data = request.get_json()
        html = read_index_html()

        activities_html = "\n\n".join(activity_output_html(a) for a in data.get("activities", []))
        units_html = "\n\n".join(unit_output_html(u) for u in data.get("units", []))
        team_html = "\n\n".join(team_output_html(t) for t in data.get("team", []))
        partners_html = "\n\n".join(partner_output_html(p) for p in data.get("partners", []))

        new_html = replace_container(html, "timeline", activities_html)
        new_html = replace_container(new_html, "units-grid", units_html)
        new_html = replace_container(new_html, "team-grid", team_html)
        new_html = replace_container(new_html, "partners-grid", partners_html)
        new_html = replace_gallery_photos(new_html, data.get("gallery", []))
        new_html = replace_stats(new_html, data.get("stats", []))

        write_index_html_safely(new_html, context="structured save")
        return jsonify({"ok": True, "path": os.path.abspath(INDEX_HTML_PATH)})
    except ValidationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}"}), 500


@app.route("/api/sections")
@requires_auth
def api_sections():
    try:
        html = read_index_html()
        return jsonify(list_all_sections(html))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/section/<section_id>", methods=["GET"])
@requires_auth
def api_get_section(section_id):
    html = read_index_html()
    span = find_section_span_by_id(html, section_id)
    if not span:
        return jsonify({"ok": False, "error": f"No section with id \"{section_id}\""}), 404
    inner_start, inner_end, _ = span
    return jsonify({"ok": True, "html": html[inner_start:inner_end]})


@app.route("/api/section/<section_id>", methods=["POST"])
@requires_auth
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
@requires_auth
def api_get_fullfile():
    return jsonify({"ok": True, "html": read_index_html()})


@app.route("/api/fullfile", methods=["POST"])
@requires_auth
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
# (index.html + the three image folders), never a blanket `-A`, so
# nothing unrelated in your repo gets swept in by accident.
# ============================================================
def tracked_paths():
    return [p for p in (REPO_DIR) if os.path.exists(p)]


def run_git(args):
    """Run a git command as a real argument list (never a shell string),
    so nothing typed into the commit message box can be interpreted as
    a shell command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args, cwd=REPO_DIR, capture_output=True, text=True
    )
    return result.returncode, result.stdout, result.stderr


def parse_git_status_porcelain(out):
    """Turn `git status --porcelain` output into [{path, status}], so the
    dashboard can list each changed file with a checkbox instead of a
    plain block of text."""
    labels = {
        "??": "Untracked", " M": "Modified", "M ": "Staged",
        "MM": "Modified*", "A ": "Added", "AM": "Added*",
        " D": "Deleted", "D ": "Staged", "R ": "Renamed",
        "RM": "Renamed*", "C ": "Copied", "UU": "Conflict",
    }
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        code, rest = line[:2], line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        files.append({"path": path, "status": labels.get(code, code.strip() or "Changed")})
    return files


@app.route("/api/git/status")
@requires_auth
def api_git_status():
    try:
        code, out, err = run_git(["status", "--porcelain", "--"] + tracked_paths())
        if code != 0:
            return jsonify({"ok": False, "error": err.strip() or "git status failed — is this folder actually a git repository?"})
        files = parse_git_status_porcelain(out)
        return jsonify({"ok": True, "files": files, "clean": len(files) == 0})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/git/pull-status")
@requires_auth
def api_git_pull_status():
    """Fetch from the remote (without merging) and report how many commits
    the current branch is behind/ahead of its upstream, so the dashboard
    can tell the user there are updates before they choose to pull."""
    try:
        code, out, err = run_git(["fetch"])
        if code != 0:
            return jsonify({"ok": False, "error": err.strip() or "git fetch failed — check your network connection or remote configuration."})

        code, out, err = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.strip() if code == 0 else None
        if not branch:
            return jsonify({"ok": False, "error": "Could not determine the current branch."})

        code, out, err = run_git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
        if code != 0:
            return jsonify({
                "ok": False,
                "error": f"No upstream configured for branch \"{branch}\" — set one with "
                         f"`git branch --set-upstream-to=origin/{branch}`.",
            })

        parts = out.split()
        ahead, behind = (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, 0)
        return jsonify({"ok": True, "branch": branch, "ahead": ahead, "behind": behind})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/git/pull", methods=["POST"])
@requires_auth
def api_git_pull():
    """Runs a real `git pull`. Only called when the user explicitly clicks
    Pull after seeing there are unpulled commits — nothing here happens
    automatically."""
    log = []
    try:
        code, out, err = run_git(["pull"])
        log.append(f"$ git pull\n{out}{err}".strip())
        if code != 0:
            return jsonify({
                "ok": False,
                "error": "git pull failed — see the log below. This often means local changes conflict with "
                         "the incoming commits; you may need to resolve this manually in a terminal.",
                "log": "\n\n".join(log),
            }), 500
        return jsonify({"ok": True, "log": "\n\n".join(log)})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?", "log": "\n\n".join(log)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}", "log": "\n\n".join(log)})


@app.route("/api/git/push", methods=["POST"])
@requires_auth
def api_git_push():
    log = []
    try:
        data = request.get_json()
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "Write a commit message first.", "log": ""}), 400

        files = [f for f in (data.get("files") or []) if isinstance(f, str) and f.strip()]
        if not files:
            return jsonify({"ok": False, "error": "No files selected to stage — pick at least one from the changed files list.", "log": ""}), 400

        code, out, err = run_git(["add", "--"] + files)
        log.append(f"$ git add -- {' '.join(files)}\n{out}{err}".strip())
        if code != 0:
            return jsonify({"ok": False, "error": "git add failed.", "log": "\n\n".join(log)}), 500

        code, out, err = run_git(["diff", "--cached", "--name-only"])
        if not out.strip():
            return jsonify({"ok": False, "error": "Nothing to commit — the selected file(s) match what's already committed.", "log": "\n\n".join(log)}), 400

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


def scoped_paths():
    """The files/folders this tool is allowed to touch — used to scope both
    `git log` (so history shown is relevant) and `git checkout` on revert
    (so reverting never reaches into unrelated parts of the repo)."""
    candidates = [INDEX_HTML_PATH, ACTIVITIES_IMG_DIR, UNITS_IMG_DIR,
                  TEAM_IMG_DIR, PARTNERS_IMG_DIR, IMG_DIR]
    return [p for p in candidates if os.path.exists(p)]


@app.route("/api/git/log")
@requires_auth
def api_git_log():
    """List recent pushed/committed history for the files this tool manages,
    so the dashboard can show 'here's what changed and when' with a revert
    button next to each entry."""
    try:
        sep = "\x1f"
        fmt = f"%H{sep}%h{sep}%an{sep}%ad{sep}%s"
        code, out, err = run_git(
            ["log", f"--pretty=format:{fmt}", "--date=short", "-n", "40", "--"] + scoped_paths()
        )
        if code != 0:
            return jsonify({"ok": False, "error": err.strip() or "git log failed — is this folder a git repository with commits?"})
        commits = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split(sep)
            if len(parts) != 5:
                continue
            full_hash, short_hash, author, date, subject = parts
            commits.append({"hash": full_hash, "short": short_hash, "author": author, "date": date, "message": subject})
        code2, out2, _ = run_git(["rev-parse", "HEAD"])
        head = out2.strip() if code2 == 0 else None
        return jsonify({"ok": True, "commits": commits, "head": head})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/git/revert", methods=["POST"])
@requires_auth
def api_git_revert():
    """Restore index.html + the image folders to how they looked at an
    older commit, then commit and push that restoration as a brand-new
    commit. Nothing is force-pushed and no history is rewritten — this is
    the same thing as running `git checkout <hash> -- <files>` yourself and
    pushing the result, just wired to a button. Requires no .bak file to
    exist; the old version is read straight out of git history."""
    log = []
    try:
        data = request.get_json() or {}
        commit_hash = (data.get("hash") or "").strip()
        if not re.fullmatch(r'[0-9a-fA-F]{7,40}', commit_hash or ""):
            return jsonify({"ok": False, "error": "That doesn't look like a valid commit hash.", "log": ""}), 400

        code, out, err = run_git(["cat-file", "-e", commit_hash])
        if code != 0:
            return jsonify({"ok": False, "error": f"Commit {commit_hash} was not found in this repo.", "log": ""}), 404

        paths = scoped_paths()

        code, out, err = run_git(["checkout", commit_hash, "--"] + paths)
        log.append(f"$ git checkout {commit_hash} -- {' '.join(paths)}\n{out}{err}".strip())
        if code != 0:
            return jsonify({"ok": False, "error": "git checkout failed — see log below.", "log": "\n\n".join(log)}), 500

        # Safety net: if the restored index.html is somehow malformed, undo
        # the checkout immediately rather than leaving a broken file on disk.
        try:
            validate_html(read_index_html(), context="revert")
        except ValidationError as e:
            run_git(["checkout", "HEAD", "--"] + paths)
            return jsonify({
                "ok": False,
                "error": f"Refused — the restored version failed validation ({e}). The checkout was undone.",
                "log": "\n\n".join(log),
            }), 400

        code, out, err = run_git(["add", "--"] + paths)
        log.append(f"$ git add -- {' '.join(paths)}\n{out}{err}".strip())

        code, out, err = run_git(["diff", "--cached", "--name-only"])
        if not out.strip():
            return jsonify({"ok": False, "error": "That version is identical to what's already here — nothing to revert.", "log": "\n\n".join(log)}), 400

        short = commit_hash[:7]
        message = f"Revert to {short}"
        code, out, err = run_git(["commit", "-m", message])
        log.append(f"$ git commit -m \"{message}\"\n{out}{err}".strip())
        if code != 0:
            return jsonify({"ok": False, "error": "git commit failed.", "log": "\n\n".join(log)}), 500

        code, out, err = run_git(["push"])
        log.append(f"$ git push\n{out}{err}".strip())
        if code != 0:
            return jsonify({
                "ok": False,
                "error": "Reverted and committed locally, but git push failed — you can retry with `git push` yourself.",
                "log": "\n\n".join(log),
            }), 500

        return jsonify({"ok": True, "log": "\n\n".join(log)})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git executable not found — is Git installed and on your PATH?", "log": "\n\n".join(log)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}", "log": "\n\n".join(log)})


@app.route("/")
@app.route("/admin")
@requires_auth
def index():
    with open(DASHBOARD_HTML_PATH, "r", encoding="utf-8") as f:
        dashboard_html = f.read()
    return Response(dashboard_html, mimetype="text/html")

if __name__ == "__main__":
    print(f"\nIEEE FSM local admin running at http://127.0.0.1:{PORT}/admin")
    print(f"Editing: {os.path.abspath(INDEX_HTML_PATH)}\n")
    app.run(
        host="127.0.0.1",
        port=PORT,
        debug=False
    )