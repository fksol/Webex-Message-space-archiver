# -*- coding: utf-8 -*-
"""Small local browser GUI for webex-space-archive.py.

Runs a tiny HTTP server (stdlib only, no extra dependencies) on 127.0.0.1
that lets you search for a Webex space and run the archive script from a
web form instead of the command line. Each run happens in its own
throw-away folder under ./webgui_runs/ with its own config .ini, so this
never touches your existing webexspacearchive-config.ini.

Start it with:  python3 webgui.py
"""
import configparser
import datetime
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote, quote

FROZEN = getattr(sys, "frozen", False)
# Frozen (PyInstaller) build: keep run output next to the .exe, not in the
# throwaway temp folder ('_MEIPASS') the bootloader extracts bundled files to.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# Where bundled data files (webex-space-archive.py) live at runtime.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent

SCRIPT_PATH = RESOURCE_DIR / "webex-space-archive.py"
RUNS_DIR = APP_DIR / "webgui_runs"
CONFIG_FILENAME = "webexspacearchive-config.ini"
HOST = "127.0.0.1"
PORT = 8877
WORKER_FLAG = "--archive-worker"

jobs = {}
jobs_lock = threading.Lock()


def worker_command(args):
    """Build the command used to run webex-space-archive.py for one job.

    A frozen .exe has no separate python interpreter to shell out to, so
    instead it re-invokes itself with a sentinel flag; run_worker() below
    then executes the bundled script in that fresh process. In dev mode
    (plain 'python3 webgui.py') sys.executable is a real interpreter, so we
    pass this file as the script to run.
    """
    if FROZEN:
        return [sys.executable, WORKER_FLAG, *args]
    return [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG, *args]


def run_worker(args):
    import builtins
    import runpy
    # webex-space-archive.py calls the bare exit()/quit() builtins in many places.
    # Those aren't real builtins -- the 'site' module injects them at interpreter
    # startup, and a frozen (PyInstaller) executable doesn't run 'site', so they'd
    # otherwise be missing here and crash with NameError.
    def _exit(code=None):
        raise SystemExit(code)
    builtins.exit = _exit
    builtins.quit = _exit
    sys.argv = ["webex-space-archive.py", *args]
    try:
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
    except SystemExit as e:
        code = e.code
        sys.exit(0 if code is None else (code if isinstance(code, int) else 1))


def ddmmyyyy(iso_date):
    return datetime.datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d%m%Y")


def build_max_messages(form):
    mode = form.get("maxmode", "count")
    if mode == "days":
        days = str(form.get("maxdays", "")).strip()
        return f"{days}d" if days else ""
    if mode == "range":
        start = str(form.get("maxfrom", "")).strip()
        end = str(form.get("maxto", "")).strip()
        if not start:
            return ""
        start = ddmmyyyy(start)
        end = ddmmyyyy(end) if end else ""
        return f"{start}-{end}"
    count = str(form.get("maxcount", "")).strip()
    return count


def write_job_config(job_dir, form):
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg.add_section("Archive Settings")
    cfg.set("Archive Settings", "mytoken", "__set_via_WEBEX_ARCHIVE_TOKEN_env_var__")
    cfg.set("Archive Settings", "myspaceid", "")
    cfg.set("Archive Settings", "download", form.get("download", "no"))
    cfg.set("Archive Settings", "useravatar", form.get("useravatar", "no"))
    cfg.set("Archive Settings", "maxtotalmessages", build_max_messages(form))
    cfg.set("Archive Settings", "outputfilename", form.get("outputfilename", "").strip())
    cfg.set("Archive Settings", "sortoldnew", "yes" if form.get("sortoldnew", "yes") == "yes" else "no")
    cfg.set("Archive Settings", "outputjson", form.get("outputjson", "no"))
    cfg.set("Archive Settings", "dst_start", "")
    cfg.set("Archive Settings", "dst_stop", "")
    cfg.set("Archive Settings", "blurring", "yes" if form.get("blurring") else "no")
    with open(job_dir / CONFIG_FILENAME, "w", encoding="utf-8") as fh:
        cfg.write(fh)


def new_job_dir():
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True)
    return job_id, job_dir


def start_process(job_id, job_dir, args, token):
    env = os.environ.copy()
    if token:
        env["WEBEX_ARCHIVE_TOKEN"] = token
    proc = subprocess.Popen(
        worker_command(args),
        cwd=str(job_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    with jobs_lock:
        jobs[job_id] = {
            "dir": job_dir,
            "log": [],
            "done": False,
            "returncode": None,
            "html": None,
        }

    def reader():
        for line in proc.stdout:
            with jobs_lock:
                jobs[job_id]["log"].append(line.rstrip("\n"))
        proc.wait()
        html_file = next(job_dir.rglob("*.html"), None)
        with jobs_lock:
            jobs[job_id]["done"] = True
            jobs[job_id]["returncode"] = proc.returncode
            jobs[job_id]["html"] = html_file.relative_to(job_dir).as_posix() if html_file else None

    threading.Thread(target=reader, daemon=True).start()


def explain_failure(output):
    """Turn known error patterns from webex-space-archive.py's stdout into a
    readable message. Returns None if the output doesn't look like a failure
    (e.g. a search that genuinely found nothing)."""
    if "Missing library 'requests'" in output:
        return ("Auf diesem Rechner fehlt die Python-Bibliothek 'requests'. "
                "Installieren mit z.B.: sudo pacman -S python-requests "
                "(oder: pip install --user requests)")
    if "Minimum Python version" in output:
        return "Python-Version zu alt (mindestens 3.9 erforderlich)."
    if "check your Access Token" in output:
        return "Ungültiges oder abgelaufenes Token."
    if "**ERROR**" in output:
        error_lines = [ln.strip() for ln in output.splitlines() if "**ERROR**" in ln]
        return " / ".join(error_lines) if error_lines else "Unbekannter Fehler beim Ausführen des Skripts."
    if "Total number of spaces" not in output:
        return "Unerwartete Antwort des Skripts:\n" + output.strip()[-500:]
    return None


def parse_search_output(text):
    group, direct = [], []
    section = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "group ___" in line or line.strip().strip("_") == "group":
            section = "group"
        elif "direct ___" in line or "direct _" in line:
            section = "direct"
        elif line.startswith("  ") and not line.startswith("    ") and line.strip():
            title = line.strip()
            if i + 1 < len(lines) and "id:" in lines[i + 1]:
                space_id = lines[i + 1].split("id:")[1].strip()
                (group if section == "group" else direct).append({"id": space_id, "title": title})
                i += 1
        i += 1
    return group, direct


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Webex Space Archiver</title>
<style>
  :root {
    --accent: #067a6f; --accent-dark: #05564e; --bg: #f4f6f6; --card: #ffffff;
    --border: #dfe5e4; --text: #1c2624; --muted: #5c6a68; --err: #b3261e; --ok: #1e7a3d;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background: var(--accent); color: #fff; padding: 18px 24px; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header p { margin: 4px 0 0; opacity: .85; font-size: 13px; }
  main { max-width: 760px; margin: 24px auto 60px; padding: 0 16px; display: flex; flex-direction: column; gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
  .card h2 { margin: 0 0 12px; font-size: 15px; color: var(--accent-dark); }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 4px; color: var(--muted); }
  input[type=text], input[type=password], input[type=number], input[type=date], select {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; background: #fff; color: var(--text);
  }
  .row { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  .row > div { flex: 1; min-width: 160px; }
  .inline { display: flex; align-items: center; gap: 8px; }
  .radios { display: flex; gap: 16px; flex-wrap: wrap; font-size: 14px; }
  .radios label { font-weight: 400; color: var(--text); display: flex; align-items: center; gap: 5px; margin: 0; }
  button { cursor: pointer; border: none; border-radius: 6px; padding: 9px 16px; font-size: 14px; font-weight: 600; background: var(--accent); color: #fff; }
  button:hover { background: var(--accent-dark); }
  button.secondary { background: #fff; color: var(--accent-dark); border: 1px solid var(--border); }
  button:disabled { opacity: .5; cursor: default; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 4px; }
  #searchResults { margin-top: 10px; display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow-y: auto; }
  .space-opt { border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; cursor: pointer; font-size: 13.5px; display: flex; justify-content: space-between; gap: 8px; }
  .space-opt:hover { border-color: var(--accent); }
  .space-opt.selected { border-color: var(--accent); background: #ecf6f4; }
  .space-opt .type { color: var(--muted); font-size: 11px; }
  #selectedSpace { margin-top: 8px; font-size: 13px; }
  #log { background: #10201c; color: #d6f5ee; font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size: 12.5px; padding: 12px; border-radius: 8px; white-space: pre-wrap; max-height: 320px; overflow-y: auto; display: none; }
  #runStatus { font-size: 14px; margin-top: 10px; }
  #runStatus.ok { color: var(--ok); }
  #runStatus.err { color: var(--err); }
  #resultActions { margin-top: 10px; display: flex; gap: 10px; flex-wrap: wrap; }
  .spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid #ccc; border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; vertical-align: -1px; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>Webex Space Archiver</h1>
  <p>Kleine Browser-Oberfläche für webex-space-archive.py &mdash; läuft lokal auf deinem Rechner</p>
</header>
<main>

  <div class="card">
    <h2>1. Zugriffstoken</h2>
    <label for="token">Webex Personal Access Token</label>
    <input type="password" id="token" placeholder="Token von developer.webex.com" autocomplete="off">
    <div class="hint">
      Wird nur an das lokale Python-Skript weitergegeben (Umgebungsvariable), nicht gespeichert. Token sind nur 12 Stunden gültig.<br>
      Token holen: <a href="https://developer.webex.com/docs/getting-started" target="_blank" rel="noopener">developer.webex.com</a> &rarr; oben rechts einloggen &rarr; auf der Startseite im grünen Kasten "Your Personal Access Token" kopieren.
    </div>
  </div>

  <div class="card">
    <h2>2. Space finden</h2>
    <div class="row">
      <div>
        <label for="search">Suche nach Space-Name</label>
        <input type="text" id="search" placeholder="z.B. Projektteam">
      </div>
      <div style="flex:0 0 auto; align-self:flex-end;">
        <button type="button" id="searchBtn">Suchen</button>
      </div>
    </div>
    <div id="searchResults"></div>
    <div id="selectedSpace"></div>
    <label for="spaceId" style="margin-top:12px;">...oder Space-ID direkt eingeben</label>
    <input type="text" id="spaceId" placeholder="Y2lzY29zcGFyaz...">
  </div>

  <div class="card">
    <h2>3. Einstellungen</h2>
    <div class="row">
      <div>
        <label for="download">Dateien / Bilder</label>
        <select id="download">
          <option value="no">Nicht herunterladen</option>
          <option value="info">Nur Name &amp; Größe anzeigen</option>
          <option value="images">Nur Bilder herunterladen</option>
          <option value="files">Bilder &amp; Dateien herunterladen</option>
        </select>
      </div>
      <div>
        <label for="useravatar">Profilbilder</label>
        <select id="useravatar">
          <option value="no">Initialen anzeigen</option>
          <option value="link">Verlinken (Internet nötig)</option>
          <option value="download">Herunterladen</option>
        </select>
      </div>
    </div>

    <label>Anzahl Nachrichten begrenzen</label>
    <div class="radios" style="margin-bottom:8px;">
      <label><input type="radio" name="maxmode" value="count" checked> Anzahl</label>
      <label><input type="radio" name="maxmode" value="days"> Letzte X Tage</label>
      <label><input type="radio" name="maxmode" value="range"> Zeitraum</label>
    </div>
    <div class="row">
      <div id="maxmode-count"><input type="number" id="maxcount" value="1000" min="1"></div>
      <div id="maxmode-days" style="display:none"><input type="number" id="maxdays" placeholder="z.B. 60" min="1"></div>
      <div id="maxmode-range" style="display:none" class="row">
        <div><input type="date" id="maxfrom"><div class="hint">von</div></div>
        <div><input type="date" id="maxto"><div class="hint">bis (optional)</div></div>
      </div>
    </div>

    <div class="row">
      <div>
        <label for="outputfilename">Dateiname (optional)</label>
        <input type="text" id="outputfilename" placeholder="leer = Space-Name">
      </div>
      <div>
        <label for="outputjson">Zusätzliche Ausgabe</label>
        <select id="outputjson">
          <option value="no">Nur HTML</option>
          <option value="json">+ JSON</option>
          <option value="txt">+ TXT</option>
          <option value="both">+ JSON &amp; TXT</option>
        </select>
      </div>
    </div>

    <div class="row">
      <div class="inline"><input type="checkbox" id="sortoldnew" checked style="width:auto"> <label style="margin:0" for="sortoldnew">Sortierung: älteste zuerst</label></div>
      <div class="inline"><input type="checkbox" id="blurring" style="width:auto"> <label style="margin:0" for="blurring">Namen/E-Mails im HTML unkenntlich machen</label></div>
    </div>
  </div>

  <div class="card">
    <h2>4. Archivieren</h2>
    <button type="button" id="runBtn">Space archivieren</button>
    <div id="runStatus"></div>
    <pre id="log"></pre>
    <div id="resultActions"></div>
  </div>

</main>
<script>
const $ = (id) => document.getElementById(id);
let selectedSpaceId = "";

function updateMaxMode() {
  const mode = document.querySelector('input[name=maxmode]:checked').value;
  $('maxmode-count').style.display = mode === 'count' ? 'block' : 'none';
  $('maxmode-days').style.display = mode === 'days' ? 'block' : 'none';
  $('maxmode-range').style.display = mode === 'range' ? 'flex' : 'none';
}
document.querySelectorAll('input[name=maxmode]').forEach(r => r.addEventListener('change', updateMaxMode));
updateMaxMode();

function selectSpace(id, title) {
  selectedSpaceId = id;
  $('spaceId').value = id;
  $('selectedSpace').textContent = 'Ausgewählt: ' + title;
  document.querySelectorAll('.space-opt').forEach(el => el.classList.toggle('selected', el.dataset.id === id));
}
$('spaceId').addEventListener('input', () => { selectedSpaceId = $('spaceId').value.trim(); });

$('searchBtn').addEventListener('click', async () => {
  const token = $('token').value.trim();
  const search = $('search').value.trim();
  if (!token) { alert('Bitte zuerst das Token eingeben.'); return; }
  if (!search) { alert('Bitte einen Suchbegriff eingeben.'); return; }
  $('searchBtn').disabled = true;
  $('searchResults').innerHTML = '<div class="hint"><span class="spinner"></span>Suche läuft...</div>';
  try {
    const res = await fetch('/api/search', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token, search})});
    const data = await res.json();
    $('searchBtn').disabled = false;
    if (!data.ok) { $('searchResults').innerHTML = '<div class="hint" style="color:var(--err);white-space:pre-wrap">' + data.error + '</div>'; return; }
    const all = [...data.group.map(s => ({...s, type:'Gruppe'})), ...data.direct.map(s => ({...s, type:'1:1'}))];
    if (all.length === 0) { $('searchResults').innerHTML = '<div class="hint">Keine Spaces gefunden.</div>'; return; }
    $('searchResults').innerHTML = '';
    all.forEach(s => {
      const div = document.createElement('div');
      div.className = 'space-opt';
      div.dataset.id = s.id;
      div.innerHTML = '<span>' + s.title.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</span><span class="type">' + s.type + '</span>';
      div.addEventListener('click', () => selectSpace(s.id, s.title));
      $('searchResults').appendChild(div);
    });
  } catch (e) {
    $('searchBtn').disabled = false;
    $('searchResults').innerHTML = '<div class="hint" style="color:var(--err)">Fehler: ' + e + '</div>';
  }
});

function buildForm() {
  return {
    download: $('download').value,
    useravatar: $('useravatar').value,
    maxmode: document.querySelector('input[name=maxmode]:checked').value,
    maxcount: $('maxcount').value,
    maxdays: $('maxdays').value,
    maxfrom: $('maxfrom').value,
    maxto: $('maxto').value,
    outputfilename: $('outputfilename').value,
    outputjson: $('outputjson').value,
    sortoldnew: $('sortoldnew').checked ? 'yes' : 'no',
    blurring: $('blurring').checked,
  };
}

let pollTimer = null;
$('runBtn').addEventListener('click', async () => {
  const token = $('token').value.trim();
  const spaceId = $('spaceId').value.trim();
  if (!token) { alert('Bitte zuerst das Token eingeben.'); return; }
  if (!spaceId) { alert('Bitte einen Space auswählen oder die Space-ID eingeben.'); return; }
  $('runBtn').disabled = true;
  $('runStatus').className = ''; $('runStatus').innerHTML = '<span class="spinner"></span>Archivierung läuft...';
  $('log').style.display = 'block'; $('log').textContent = '';
  $('resultActions').innerHTML = '';
  try {
    const res = await fetch('/api/archive', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token, spaceId, form: buildForm()})});
    const data = await res.json();
    if (!data.ok) { $('runBtn').disabled = false; $('runStatus').className='err'; $('runStatus').textContent = data.error; return; }
    poll(data.job);
  } catch (e) {
    $('runBtn').disabled = false; $('runStatus').className='err'; $('runStatus').textContent = 'Fehler: ' + e;
  }
});

function poll(job) {
  pollTimer = setInterval(async () => {
    const res = await fetch('/api/status?job=' + job);
    const data = await res.json();
    $('log').textContent = data.log.join('\\n');
    $('log').scrollTop = $('log').scrollHeight;
    if (data.done) {
      clearInterval(pollTimer);
      $('runBtn').disabled = false;
      if (data.html) {
        $('runStatus').className = 'ok';
        $('runStatus').textContent = 'Fertig! Archiv wurde erstellt.';
        $('resultActions').innerHTML =
          '<a href="/files/' + job + '/' + data.html.split('/').map(encodeURIComponent).join('/') + '" target="_blank"><button type="button">Archiv öffnen</button></a>' +
          '<button type="button" class="secondary" id="zipBtn">Als ZIP herunterladen</button>';
        $('zipBtn').addEventListener('click', () => { window.location = '/api/zip?job=' + job; });
      } else {
        $('runStatus').className = 'err';
        $('runStatus').textContent = 'Es wurde kein Archiv erzeugt — siehe Log oben für Details.';
      }
    }
  }, 1000);
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = unquote(parts.path), parse_qs(parts.query)

        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/status":
            job_id = (query.get("job") or [""])[0]
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    self._send_json({"ok": False, "error": "unknown job"}, 404)
                    return
                self._send_json({
                    "ok": True, "done": job["done"], "returncode": job["returncode"],
                    "log": job["log"], "html": job["html"],
                })
            return

        if path == "/api/zip":
            job_id = (query.get("job") or [""])[0]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job or not job.get("html"):
                self.send_response(404)
                self.end_headers()
                return
            archive_dir = (job["dir"] / job["html"]).parent
            zip_base = RUNS_DIR / f"{job_id}-archive"
            zip_path = Path(shutil.make_archive(str(zip_base), "zip", root_dir=str(archive_dir)))
            data = zip_path.read_bytes()
            zip_path.unlink(missing_ok=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{archive_dir.name}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/files/"):
            self._serve_file(path[len("/files/"):])
            return

        self.send_response(404)
        self.end_headers()

    def _serve_file(self, rel):
        try:
            job_id, sub = rel.split("/", 1)
        except ValueError:
            self.send_response(404)
            self.end_headers()
            return
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            self.send_response(404)
            self.end_headers()
            return
        job_dir = job["dir"].resolve()
        target = (job_dir / sub).resolve()
        if job_dir not in target.parents and target != job_dir:
            self.send_response(403)
            self.end_headers()
            return
        if not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parts = urlsplit(self.path)
        path = parts.path

        if path == "/api/search":
            payload = self._read_json()
            token = (payload.get("token") or "").strip()
            search = (payload.get("search") or "").strip()
            if not token or not search:
                self._send_json({"ok": False, "error": "Token und Suchbegriff sind erforderlich."})
                return
            job_id, job_dir = new_job_dir()
            write_job_config(job_dir, {})
            env = os.environ.copy()
            env["WEBEX_ARCHIVE_TOKEN"] = token
            try:
                result = subprocess.run(
                    worker_command([search]),
                    cwd=str(job_dir), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=120,
                )
                output = result.stdout
            except subprocess.TimeoutExpired:
                self._send_json({"ok": False, "error": "Zeitüberschreitung bei der Suche."})
                return
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
            group, direct = parse_search_output(output)
            if not group and not direct:
                err = explain_failure(output)
                if err:
                    self._send_json({"ok": False, "error": err})
                    return
            self._send_json({"ok": True, "group": group, "direct": direct})
            return

        if path == "/api/archive":
            payload = self._read_json()
            token = (payload.get("token") or "").strip()
            space_id = (payload.get("spaceId") or "").strip()
            form = payload.get("form") or {}
            if not token or not space_id:
                self._send_json({"ok": False, "error": "Token und Space-ID sind erforderlich."})
                return
            job_id, job_dir = new_job_dir()
            write_job_config(job_dir, form)
            start_process(job_id, job_dir, [CONFIG_FILENAME, space_id], token)
            self._send_json({"ok": True, "job": job_id})
            return

        self.send_response(404)
        self.end_headers()


def main():
    if WORKER_FLAG in sys.argv:
        run_worker(sys.argv[sys.argv.index(WORKER_FLAG) + 1:])
        return
    if not SCRIPT_PATH.is_file():
        print(f"ERROR: {SCRIPT_PATH} not found.")
        sys.exit(1)
    RUNS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Webex Space Archiver GUI läuft auf {url}")
    print("Zum Beenden: Strg+C")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
