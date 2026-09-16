#!/usr/bin/env python3
"""
Army Pubs Dashboard
===================

Reads a list of Army Publishing Directorate (APD) publication detail pages,
pulls the Pub/Form Number, Pub/Form Date, Pub/Form Title, and the
Unit Of Issue(s) download link from each one, and writes a single
dashboard.html you can open in your browser.

It remembers the last date it saw for every pub (in pubs_state.json). The
next time you run it, if a pub's date has changed, that row is flagged
"UPDATED" and the new date plus the new download link are pulled in
automatically.

Run it once a day. See README.md for setup and scheduling.

Usage:
    python3 army_pubs_dashboard.py            # check all pubs, write dashboard.html
    python3 army_pubs_dashboard.py --open     # also open the dashboard when done
    python3 army_pubs_dashboard.py --pubs other_list.txt
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import webbrowser
from urllib.parse import urljoin, urlparse, parse_qs

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing a dependency. Run:  pip3 install requests beautifulsoup4")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PUBS_FILE = os.path.join(SCRIPT_DIR, "pubs.txt")
STATE_FILE = os.path.join(SCRIPT_DIR, "pubs_state.json")
DEFAULT_OUT_FILE = os.path.join(SCRIPT_DIR, "dashboard.html")
NOTIFY_URL_FILE = os.path.join(SCRIPT_DIR, "notify_url.txt")

# JavaScript for the batch email signup. %s is replaced with the JSON-encoded
# Google Apps Script web app URL. Its braces are literal (not run through
# str.format), so no doubling is needed here.
NOTIFY_JS_TEMPLATE = """
<script>
(function(){
  var NOTIFY_URL = %s;
  var all = document.getElementById('check-all');
  if (all) all.addEventListener('change', function(){
    document.querySelectorAll('.pub-check').forEach(function(c){
      var row = c.closest('tr');
      if (row && row.style.display === 'none') return;  // skip rows hidden by the active tab
      c.checked = all.checked;
    });
  });
  var go = document.getElementById('sub-go');
  if (go) go.addEventListener('click', function(){
    var input = document.getElementById('sub-email');
    var msg = document.getElementById('sub-msg');
    var email = ((input && input.value) || '').trim();
    if (!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(email)) {
      msg.className = 'notify-msg err';
      msg.textContent = 'Please enter a valid email.';
      return;
    }
    var checked = document.querySelectorAll('.pub-check:checked');
    if (!checked.length) {
      msg.className = 'notify-msg err';
      msg.textContent = 'Check at least one pub first.';
      return;
    }
    var pubs = Array.prototype.map.call(checked, function(c){
      return {
        pub_id: c.getAttribute('data-pub'),
        number: c.getAttribute('data-number') || '',
        title: c.getAttribute('data-title') || ''
      };
    });
    go.disabled = true;
    fetch(NOTIFY_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ email: email, pubs: pubs })
    }).then(function(){
      msg.className = 'notify-msg';
      msg.textContent = 'Almost done. Check ' + email +
        ' for one confirmation link covering your ' + pubs.length + ' selected pub(s).';
      if (input) input.value = '';
      document.querySelectorAll('.pub-check').forEach(function(c){ c.checked = false; });
      if (all) all.checked = false;
    }).catch(function(){
      msg.className = 'notify-msg err';
      msg.textContent = 'Something went wrong. Please try again.';
    }).then(function(){ go.disabled = false; });
  });
})();
</script>
"""


def read_notify_url():
    """
    The Google Apps Script web app URL. Read from the NOTIFY_URL environment
    variable, or from notify_url.txt next to this script. Empty means the
    signup buttons are left off the page.
    """
    env = os.environ.get("NOTIFY_URL", "").strip()
    if env:
        return env
    if os.path.exists(NOTIFY_URL_FILE):
        with open(NOTIFY_URL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return ""

DETAIL_URL = "https://armypubs.army.mil/ProductMaps/PubForm/Details.aspx?PUB_ID={}"

# A normal browser User-Agent. The APD pages are server-rendered, so a plain
# request receives the full table without needing JavaScript.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Seconds to wait between requests, to stay polite to the server.
REQUEST_DELAY = 1.5

# Row labels on the APD detail page, normalized to lowercase. If APD ever
# renames a label, adjust the value on the right to match the new wording.
LABELS = {
    "number": "pub/form number",
    "date": "pub/form date",
    "title": "pub/form title",
    "uoi": "unit of issue(s)",
    "status": "pub/form status",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def norm(text):
    """Collapse whitespace and lowercase, for reliable label matching."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def today_str():
    return datetime.date.today().isoformat()


def extract_pub_id(line):
    """
    Turn one line of pubs.txt into a PUB_ID.
    A line may be a bare id (1003624) or a full Details.aspx URL.
    Returns None for blanks and comment lines starting with #.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.isdigit():
        return line
    # Full URL: pull PUB_ID from the query string.
    try:
        q = parse_qs(urlparse(line).query)
        if "PUB_ID" in q and q["PUB_ID"]:
            return q["PUB_ID"][0]
    except Exception:
        pass
    m = re.search(r"PUB_ID=(\d+)", line, re.IGNORECASE)
    if m:
        return m.group(1)
    # Last resort: first long run of digits on the line.
    m = re.search(r"(\d{3,})", line)
    if m:
        return m.group(1)
    return None


def read_pubs(path):
    """Read pubs.txt into an ordered, de-duplicated list of PUB_IDs."""
    if not os.path.exists(path):
        print("Could not find {}. Create it with one pub per line.".format(path))
        sys.exit(1)
    ids = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            pub_id = extract_pub_id(raw)
            if pub_id and pub_id not in seen:
                seen.add(pub_id)
                ids.append(pub_id)
    return ids


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            print("Warning: pubs_state.json was unreadable, starting fresh.")
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Fetch and parse
# ---------------------------------------------------------------------------

def fetch(pub_id, session):
    url = DETAIL_URL.format(pub_id)
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return url, resp.text


def parse_detail(html, page_url):
    """
    Pull the fields we care about out of an APD detail page.

    The page lays each field out as a two-column table row:
        <tr><td>Pub/Form Date</td><td>03/22/2018</td></tr>
    We walk every row, match the left cell against a known label, and read
    the right cell. For Unit Of Issue(s) we grab the actual links.
    """
    soup = BeautifulSoup(html, "html.parser")
    rec = {"number": "", "date": "", "title": "", "status": "", "uoi": []}

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = norm(cells[0].get_text())
        value_cell = cells[1]

        if label == LABELS["number"]:
            rec["number"] = value_cell.get_text(strip=True)
        elif label == LABELS["date"]:
            rec["date"] = value_cell.get_text(strip=True)
        elif label == LABELS["title"]:
            rec["title"] = value_cell.get_text(strip=True)
        elif label == LABELS["status"]:
            rec["status"] = value_cell.get_text(strip=True)
        elif label == LABELS["uoi"]:
            links = []
            for a in value_cell.find_all("a"):
                href = (a.get("href") or "").strip()
                text = a.get_text(strip=True) or "Download"
                if href:
                    links.append({"text": text, "href": urljoin(page_url, href)})
            if not links:
                txt = value_cell.get_text(strip=True)
                if txt:
                    links.append({"text": txt, "href": ""})
            rec["uoi"] = links

    # Fallback for the number if the table row was missed: the page heading
    # reads "Record Details for AR 95-1".
    if not rec["number"]:
        heading = soup.find(string=re.compile(r"Record Details for", re.IGNORECASE))
        if heading:
            m = re.search(r"Record Details for\s+(.+)", heading.strip(), re.IGNORECASE)
            if m:
                rec["number"] = m.group(1).strip()

    return rec


# ---------------------------------------------------------------------------
# Main check routine
# ---------------------------------------------------------------------------

def check_all(pub_ids, state):
    """
    Check every pub, update state, and return a list of display rows in the
    same order as pubs.txt. Each display row carries transient flags
    (updated / new / error) used only for this run's dashboard.
    """
    session = requests.Session()
    display = []
    today = today_str()

    for i, pub_id in enumerate(pub_ids):
        prev = state.get(pub_id)
        row = {"pub_id": pub_id, "url": DETAIL_URL.format(pub_id)}

        try:
            url, html = fetch(pub_id, session)
            rec = parse_detail(html, url)

            is_new = prev is None
            date_changed = (not is_new) and prev.get("date", "") != rec["date"] and rec["date"] != ""

            entry = {
                "pub_id": pub_id,
                "url": url,
                "number": rec["number"] or (prev.get("number", "") if prev else ""),
                "date": rec["date"] or (prev.get("date", "") if prev else ""),
                "title": rec["title"] or (prev.get("title", "") if prev else ""),
                "status": rec["status"] or (prev.get("status", "") if prev else ""),
                "uoi": rec["uoi"] if rec["uoi"] else (prev.get("uoi", []) if prev else []),
                "first_seen": prev.get("first_seen", today) if prev else today,
                "last_checked": today,
                "last_changed": today if (is_new or date_changed) else (prev.get("last_changed", today) if prev else today),
                "history": prev.get("history", []) if prev else [],
            }
            if is_new or date_changed:
                entry["history"].append({"date": rec["date"], "seen": today})

            state[pub_id] = entry

            row.update(entry)
            row["flag"] = "new" if is_new else ("updated" if date_changed else "ok")
            print("  [{}/{}] {}  {}  {}".format(
                i + 1, len(pub_ids),
                (entry["number"] or pub_id).ljust(14),
                (entry["date"] or "?").ljust(12),
                row["flag"].upper()))

        except Exception as e:
            # Keep whatever we already knew, flag the row as a failed check.
            if prev:
                row.update(prev)
            row["flag"] = "error"
            row["error"] = str(e)
            row.setdefault("number", "")
            row.setdefault("date", "")
            row.setdefault("title", "")
            row.setdefault("status", "")
            row.setdefault("uoi", [])
            row["last_checked"] = prev.get("last_checked", "never") if prev else "never"
            print("  [{}/{}] {}  CHECK FAILED: {}".format(i + 1, len(pub_ids), pub_id, e))

        display.append(row)
        if i < len(pub_ids) - 1:
            time.sleep(REQUEST_DELAY)

    return display


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

def esc(text):
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_uoi(links):
    if not links:
        return '<span class="muted">n/a</span>'
    parts = []
    for lk in links:
        if lk.get("href"):
            parts.append('<a href="{}" target="_blank" rel="noopener">{}</a>'.format(
                esc(lk["href"]), esc(lk["text"])))
        else:
            parts.append(esc(lk["text"]))
    return ", ".join(parts)


def render_badge(flag):
    if flag == "updated":
        return '<span class="badge badge-updated">UPDATED</span>'
    if flag == "new":
        return '<span class="badge badge-new">NEW</span>'
    if flag == "error":
        return '<span class="badge badge-error">CHECK FAILED</span>'
    return ""


def pub_category(number, pub_id):
    """
    Derive the tab/category from a pub number: the leading letter-only tokens
    before the first token that contains a digit. "AR 95-1" -> "AR",
    "ATP 3-04.1" -> "ATP", "DA FORM 7305" -> "DA FORM", "DA PAM 385-64" -> "DA PAM".
    """
    s = (number or "").strip()
    if not s:
        return "Other"
    tokens = s.split()
    prefix = []
    for t in tokens:
        if any(ch.isdigit() for ch in t):
            break
        prefix.append(t)
    if not prefix:
        return tokens[0].upper() if tokens else "Other"
    return " ".join(prefix).upper()


# Tab filtering. Included on every page (works with or without the signup UI).
TABS_JS = """
<script>
(function(){
  var tabs = document.querySelectorAll('.tab');
  tabs.forEach(function(t){
    t.addEventListener('click', function(){
      tabs.forEach(function(x){ x.classList.remove('active'); });
      t.classList.add('active');
      var cat = t.getAttribute('data-cat');
      document.querySelectorAll('tbody tr').forEach(function(row){
        var show = (cat === '__all__') || (row.getAttribute('data-cat') === cat);
        row.style.display = show ? '' : 'none';
      });
      var all = document.getElementById('check-all');
      if (all) all.checked = false;
    });
  });
})();
</script>
"""


def build_html(display, generated_at, notify_url=""):
    updated = [r for r in display if r.get("flag") == "updated"]
    errors = [r for r in display if r.get("flag") == "error"]
    notify_on = bool(notify_url)

    banner = ""
    if updated:
        names = ", ".join(esc(r.get("number") or r["pub_id"]) for r in updated)
        banner += ('<div class="alert alert-updated">'
                   '{} publication(s) changed since the last check: {}'
                   '</div>').format(len(updated), names)
    if errors:
        names = ", ".join(esc(r.get("number") or r["pub_id"]) for r in errors)
        banner += ('<div class="alert alert-error">'
                   'Could not reach {} publication(s) this run (showing last known data): {}'
                   '</div>').format(len(errors), names)

    rows = []
    cat_counts = {}
    for r in display:
        flag = r.get("flag", "ok")
        row_class = " class=\"row-updated\"" if flag == "updated" else (
            " class=\"row-error\"" if flag == "error" else "")
        cat = pub_category(r.get("number"), r["pub_id"])
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        number_cell = esc(r.get("number") or r["pub_id"])
        number_link = '<a href="{}" target="_blank" rel="noopener">{}</a>'.format(
            esc(r["url"]), number_cell)
        notify_cell = ""
        if notify_on:
            pid = esc(r["pub_id"])
            notify_cell = (
                "<td class=\"notify\">"
                "<input type=\"checkbox\" class=\"pub-check\" data-pub=\"{pid}\" "
                "data-number=\"{dnum}\" data-title=\"{dtitle}\" "
                "aria-label=\"Get alerts for {dnum}\">"
                "</td>"
            ).format(
                pid=pid,
                dnum=esc(r.get("number") or r["pub_id"]),
                dtitle=esc(r.get("title") or ""),
            )
        rows.append(
            "<tr{cls} data-cat=\"{cat}\">"
            "<td class=\"num\">{num} {badge}</td>"
            "<td class=\"date\">{date}</td>"
            "<td class=\"title\">{title}</td>"
            "<td class=\"uoi\">{uoi}</td>"
            "<td class=\"status\">{status}</td>"
            "<td class=\"checked\">{checked}</td>"
            "{notify}"
            "</tr>".format(
                cls=row_class,
                cat=esc(cat),
                num=number_link,
                badge=render_badge(flag),
                date=esc(r.get("date") or "?"),
                title=esc(r.get("title") or ""),
                uoi=render_uoi(r.get("uoi") or []),
                status=esc(r.get("status") or ""),
                checked=esc(r.get("last_checked") or ""),
                notify=notify_cell,
            ))

    # Tabs: All first, then each category alphabetically, with counts.
    tab_buttons = ['<button class="tab active" data-cat="__all__">All ({})</button>'.format(len(display))]
    for cat in sorted(cat_counts):
        tab_buttons.append('<button class="tab" data-cat="{c}">{c} ({n})</button>'.format(
            c=esc(cat), n=cat_counts[cat]))
    tabs = '<div class="tabs">' + "".join(tab_buttons) + '</div>' if display else ""

    notify_js = NOTIFY_JS_TEMPLATE % json.dumps(notify_url) if notify_on else ""
    subscribe_bar = ""
    if notify_on:
        subscribe_bar = (
            "<div class=\"subscribe-bar\">"
            "<span class=\"sb-label\">Get email alerts when a pub changes: "
            "check the ones you want, enter your email, and subscribe.</span>"
            "<div class=\"sb-row\">"
            "<input type=\"email\" id=\"sub-email\" placeholder=\"you@example.com\" "
            "autocomplete=\"email\">"
            "<button id=\"sub-go\">Subscribe to checked</button>"
            "</div>"
            "<div id=\"sub-msg\" class=\"notify-msg\"></div>"
            "</div>"
        )

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Army Pubs Dashboard</title>
<style>
  :root {{
    --gold: #b6a269;
    --dark: #1c1c1c;
    --line: #d9d9d9;
    --updated: #e7f6ea;
    --updated-bar: #2e7d32;
    --error: #fdecea;
    --error-bar: #c62828;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    color: #202020;
    background: #f4f4f4;
  }}
  header {{
    background: var(--dark);
    color: #fff;
    padding: 22px 28px;
    border-bottom: 5px solid var(--gold);
  }}
  header h1 {{ margin: 0; font-size: 22px; letter-spacing: .5px; }}
  header .sub {{ color: var(--gold); font-size: 13px; margin-top: 4px; }}
  main {{ max-width: 1150px; margin: 0 auto; padding: 22px; }}
  .alert {{ padding: 12px 16px; border-radius: 6px; margin-bottom: 14px; font-size: 14px; }}
  .alert-updated {{ background: var(--updated); border-left: 5px solid var(--updated-bar); }}
  .alert-error {{ background: var(--error); border-left: 5px solid var(--error-bar); }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           box-shadow: 0 1px 3px rgba(0,0,0,.12); border-radius: 6px; overflow: hidden; }}
  th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line);
            font-size: 14px; vertical-align: top; }}
  th {{ background: var(--gold); color: #1c1c1c; font-weight: 700;
        text-transform: uppercase; font-size: 12px; letter-spacing: .4px; }}
  tr:last-child td {{ border-bottom: none; }}
  .num a {{ font-weight: 700; color: #0b3d91; text-decoration: none; }}
  .num a:hover {{ text-decoration: underline; }}
  .uoi a {{ color: #0b3d91; }}
  .row-updated {{ background: var(--updated); }}
  .row-error {{ background: var(--error); }}
  .badge {{ display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 7px;
            border-radius: 10px; margin-left: 6px; vertical-align: middle; letter-spacing: .3px; }}
  .badge-updated {{ background: var(--updated-bar); color: #fff; }}
  .badge-new {{ background: #0b3d91; color: #fff; }}
  .badge-error {{ background: var(--error-bar); color: #fff; }}
  .muted {{ color: #999; }}
  footer {{ max-width: 1150px; margin: 0 auto; padding: 8px 22px 30px;
            color: #777; font-size: 12px; }}
  .notify-msg {{ font-size: 12px; margin-top: 6px; color: #2e7d32; }}
  .notify-msg.err {{ color: #c62828; }}
  .subscribe-bar {{ background: #fff; border: 1px solid var(--line); border-radius: 6px;
            padding: 14px 16px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .subscribe-bar .sb-label {{ font-size: 14px; display: block; margin-bottom: 10px; }}
  .subscribe-bar .sb-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  #sub-email {{ padding: 8px 10px; font-size: 14px; border: 1px solid #bbb;
            border-radius: 5px; width: 240px; max-width: 100%; }}
  #sub-go {{ background: #0b3d91; color: #fff; border: none; border-radius: 5px;
            padding: 8px 14px; font-size: 14px; cursor: pointer; }}
  #sub-go:hover {{ background: #092f70; }}
  .pub-check {{ width: 18px; height: 18px; cursor: pointer; }}
  td.notify, th.notify {{ text-align: center; }}
  .tabs {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }}
  .tab {{ background: #fff; border: 1px solid var(--line); border-radius: 999px;
          padding: 7px 14px; font-size: 13px; font-weight: 600; color: #333;
          cursor: pointer; }}
  .tab:hover {{ border-color: #b0b0b0; }}
  .tab.active {{ background: var(--dark); color: #fff; border-color: var(--dark); }}
</style>
</head>
<body>
<header>
  <h1>Army Pubs Dashboard</h1>
  <div class="sub">Tracking {count} publication(s) from the Army Publishing Directorate</div>
</header>
<main>
  {banner}
  {subscribe_bar}
  {tabs}
  <table>
    <thead>
      <tr>
        <th>Pub/Form Number</th>
        <th>Pub/Form Date</th>
        <th>Pub/Form Title</th>
        <th>Unit Of Issue(s)</th>
        <th>Status</th>
        <th>Last Checked</th>
        {notify_th}
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</main>
<footer>
  Generated {generated}. Data pulled from armypubs.army.mil. The Number links back to
  each source page. Re-run the script to refresh; changed dates are flagged UPDATED.
</footer>
{tabs_js}
{notify_js}
</body>
</html>
""".format(
        count=len(display),
        banner=banner,
        subscribe_bar=subscribe_bar,
        tabs=tabs,
        rows="\n      ".join(rows),
        generated=esc(generated_at),
        notify_th=('<th class="notify">Alert<br><input type="checkbox" id="check-all" '
                   'aria-label="Select all pubs"></th>' if notify_on else ""),
        tabs_js=(TABS_JS if display else ""),
        notify_js=notify_js,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Army Pubs Dashboard")
    ap.add_argument("--pubs", default=DEFAULT_PUBS_FILE, help="path to the pubs list")
    ap.add_argument("--out", default=DEFAULT_OUT_FILE, help="path to the output html")
    ap.add_argument("--open", action="store_true", help="open the dashboard when done")
    args = ap.parse_args()

    pub_ids = read_pubs(args.pubs)
    if not pub_ids:
        print("No pubs found in {}. Add at least one URL or PUB_ID.".format(args.pubs))
        sys.exit(1)

    print("Checking {} publication(s)...".format(len(pub_ids)))
    state = load_state()
    display = check_all(pub_ids, state)
    save_state(state)

    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = build_html(display, generated_at, notify_url=read_notify_url())
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print("\nDashboard written to {}".format(args.out))
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))


if __name__ == "__main__":
    main()
