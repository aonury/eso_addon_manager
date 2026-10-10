#!/usr/bin/env python3
"""Minimal ESO addon manager: list, search, install, update and remove ESOUI addons, from the CLI or a local web page."""
import argparse
import contextlib
import functools
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BACKUP_DIR = ".eso-addons-backup"
# ESOUI version we installed; some authors forget to bump ## Version in the zip
VERSION_MARK = ".eso-addons-version"
# Tells ESOUI who is calling and where to reach the maintainer
HEADERS = {"User-Agent": "eso_addon_manager/1.0 (+https://github.com/aonury/eso_addon_manager)"}
CONFIG_PATH = Path.home() / ".config" / "eso-addons" / "config.json"
CACHE_PATH = Path.home() / ".cache" / "eso-addons" / "filelist.json"
API = "https://api.mmoui.com/v3/game/ESO"


def candidate_dirs():
    home = Path.home()
    rel = Path("Elder Scrolls Online") / "live" / "AddOns"
    cands = [home / "Documents" / rel, home / "OneDrive" / "Documents" / rel]
    # Linux: Steam + Proton (ESO app id 306130); native and Flatpak Steam
    prefix = "steamapps/compatdata/306130/pfx/drive_c/users/steamuser/Documents"
    for steam in (
        ".steam/steam",
        ".local/share/Steam",
        ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ".var/app/com.valvesoftware.Steam/.steam/steam",
    ):
        cands.append(home / steam / prefix / rel)
    return cands


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_addons_dir():
    env = os.environ.get("ESO_ADDONS_DIR")
    if env:
        return Path(env)
    cfg = load_config()
    if cfg.get("addons_dir"):
        return Path(cfg["addons_dir"])
    for c in candidate_dirs():
        if c.is_dir():
            return c
    sys.exit("AddOns folder not found. Run: eso_addons.py config --dir <path>")


COLOR = re.compile(r"\|c[0-9A-Fa-f]{6}|\|r")


def parse_manifest(folder: Path):
    """Read ## key: value lines from <folder>/<folder>.txt (or .addon)."""
    for ext in (".txt", ".addon"):
        f = folder / (folder.name + ext)
        if f.is_file():
            break
    else:
        return None
    meta = {"folder": folder.name, "depends": []}
    for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        m = re.match(r"##\s*([A-Za-z]+)\s*:\s*(.*)", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "title":
            meta["title"] = COLOR.sub("", val)
        elif key == "version":
            meta["version"] = val
        elif key == "author":
            meta["author"] = COLOR.sub("", val)
        elif key == "dependson":
            meta["depends"] = [re.split(r"[<>=]", d)[0] for d in val.split()]
        elif key == "islibrary":
            meta["lib"] = val.lower() == "true"
    meta.setdefault("title", folder.name)
    meta.setdefault("version", "?")
    return meta


def addon_meta(d: Path):
    """One top-level folder as one addon.

    ESO also loads addons nested up to two levels down (HarvestMap/Modules/HarvestMap), so their
    version, dependencies and titles are folded into the top folder's entry.
    """
    m = parse_manifest(d)
    nested = [n for p in (*d.glob("*"), *d.glob("*/*")) if p.is_dir() and (n := parse_manifest(p))]
    if not m and not nested:
        return None
    m = m or {"folder": d.name, "title": d.name, "version": "?", "depends": []}
    if m["version"] == "?":  # stub manifest at the top, real one nested under the same name
        m["version"] = next((n["version"] for n in nested if n["folder"].lower() == d.name.lower()), "?")
    inside = {d.name} | {n["folder"] for n in nested}
    m["depends"] = sorted({x for a in (m, *nested) for x in a["depends"]} - inside)
    m["includes"] = sorted({n["title"] for n in nested if n["folder"].lower() != d.name.lower()}, key=str.lower)
    mark = d / VERSION_MARK
    m["installed"] = mark.read_text(encoding="utf-8").strip() if mark.is_file() else ""
    if m["version"] == "?" and m["installed"]:  # no ## Version line (HarvestPins), but we installed it
        m["version"] = m["installed"]
    return m


def installed_addons():
    root = get_addons_dir()
    out = []
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if d.is_dir() and d.name != BACKUP_DIR and (m := addon_meta(d)):
            out.append(m)
    return out


def catalog_age():
    """Seconds since the catalog was downloaded, inf when it never was."""
    return time.time() - CACHE_PATH.stat().st_mtime if CACHE_PATH.exists() else float("inf")


def catalog(max_age=3600):
    """ESOUI file list, cached 1 hour by default (the list is several MB)."""
    fresh = catalog_age() < max_age
    if not fresh:
        try:
            data = http_get(API + "/filelist.json", timeout=30)
            json.loads(data)  # a broken download must not replace the cache and fail every page for an hour
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_bytes(data)
        except (OSError, ValueError) as e:
            if not CACHE_PATH.exists():
                sys.exit(f"Could not download catalog: {e}")
            print(f"Warning: download failed ({e}), using stale cache", file=sys.stderr)
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))


def find(terms):
    """Catalog entries whose name or folders contain every term, most downloaded first."""
    terms = [t.lower() for t in terms]
    hits = [
        f for f in catalog()
        if all(t in (f["UIName"] + " " + " ".join(f.get("UIDir") or [])).lower() for t in terms)
    ]
    hits.sort(key=lambda f: int(f.get("UIDownloadMonthly") or 0), reverse=True)
    return hits


@functools.lru_cache(maxsize=1)
def categories():
    """ESOUI category id -> title, fetched once per run."""
    return {c["UICATID"]: c["UICATTitle"].strip() for c in json.loads(http_get(API + "/categorylist.json"))}


def cmd_search(args):
    hits = find(args.terms)
    for f in hits[: args.limit]:
        print(f"{f['UID']:>5}  {f['UIVersion']:<14} {f['UIName']}  (monthly {f.get('UIDownloadMonthly', '0')})")
    print(f"\n{len(hits)} results", file=sys.stderr)


def dir_index():
    """Folder name -> catalog entry."""
    by_dir = {}
    for f in catalog():
        for d in f.get("UIDir") or []:
            cur = by_dir.get(d)
            # ponytail: a folder shipped by several ESOUI files maps to the most downloaded one;
            # ask the user to pick when wrong matches show up
            if cur is None or int(f.get("UIDownloadMonthly") or 0) > int(cur.get("UIDownloadMonthly") or 0):
                by_dir[d] = f
    return by_dir


def match_installed():
    """Map installed folders to catalog entries via UIDir -> ([(entry, manifest)], unmatched)."""
    by_dir = dir_index()
    found, unmatched = {}, []
    for a in installed_addons():
        f = by_dir.get(a["folder"])
        if f is None:
            unmatched.append(a)
        else:
            # ponytail: multi-folder addons compare only the first installed folder's version
            found.setdefault(f["UID"], (f, a))
    return list(found.values()), unmatched


def ver_key(v):
    """'v0.79' -> (0, 79); '1.0 r47' -> (1, 0, 47).

    ponytail: digits only, so 'beta'/'rc' are ignored; use packaging.version if that ever misorders.
    """
    return tuple(int(x) for x in re.findall(r"\d+", v))


def is_outdated(a, f):
    """Newer of manifest version and our install mark, compared with ESOUI.

    An unreadable version ("?") gives an empty key, which sorts below everything and so counts as outdated.
    """
    return max(ver_key(a["version"]), ver_key(a["installed"])) < ver_key(f["UIVersion"])


def cmd_outdated(args):
    pairs, unmatched = match_installed()
    n = 0
    for f, a in pairs:
        if is_outdated(a, f):
            print(f"{f['UID']:>5}  {a['folder']:<28} {a['version']:<16} -> {f['UIVersion']}")
            n += 1
    print(f"\n{n} outdated, {len(pairs)} matched, {len(unmatched)} not on ESOUI", file=sys.stderr)
    if args.unmatched and unmatched:
        print("\nNot on ESOUI:", ", ".join(a["folder"] for a in unmatched), file=sys.stderr)


def http_get(url, timeout=60):
    # URLs come from the ESOUI API; urllib would also open file:// and the like
    if not url.startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS URL: {url}")
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout).read()


def move_to_backup(root, name, stamp):
    dest = root / BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root / name), str(dest / name))


def install_one(uid, root, stamp, by_dir, done):
    """Download, verify, back up what it replaces, extract, then install missing dependencies."""
    if uid in done:
        return
    done.add(uid)
    d = json.loads(http_get(f"{API}/filedetails/{uid}.json"))[0]
    data = http_get(d["UIDownload"])
    if d.get("UIMD5") and hashlib.md5(data).hexdigest() != d["UIMD5"]:
        sys.exit(f"MD5 mismatch for {d['UIName']}: download corrupted, AddOns not touched for it")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        base = root.resolve()
        for n in names:  # zip comes from a third party: refuse anything that escapes AddOns
            if base not in (base / n).resolve().parents:
                sys.exit(f"Unsafe path in zip for {d['UIName']}: {n}")
        tops = sorted({n.split("/")[0] for n in names} - {""})
        if BACKUP_DIR in tops:
            sys.exit(f"Unsafe path in zip for {d['UIName']}: {BACKUP_DIR}")
        for t in tops:
            if (root / t).exists():
                move_to_backup(root, t, stamp)
        z.extractall(root)
    for t in tops:
        if (root / t).is_dir():
            (root / t / VERSION_MARK).write_text(d["UIVersion"], encoding="utf-8")
    print(f"installed {d['UIName']} {d['UIVersion']}: {', '.join(tops)}")
    for t in tops:
        m = addon_meta(root / t)
        for dep in m["depends"] if m else []:
            if (root / dep).is_dir():
                continue
            f = by_dir.get(dep)
            if f:
                install_one(f["UID"], root, stamp, by_dir, done)
            else:
                print(f"warning: dependency {dep} not found on ESOUI", file=sys.stderr)


def cmd_install(args):
    root = get_addons_dir()
    by_dir = dir_index()
    stamp, done = time.strftime("%Y%m%d-%H%M%S"), set()
    for a in args.targets:
        if a.isascii() and a.isdigit():
            uid = a
        elif a in by_dir:
            uid = by_dir[a]["UID"]
        else:
            sys.exit(f"'{a}' is not an ESOUI ID or a known folder name; try: search {a}")
        install_one(uid, root, stamp, by_dir, done)


def required_by(addons):
    """Folder -> installed addons that list it in DependsOn."""
    req = {}
    for a in addons:
        for dep in a["depends"]:
            if dep != a["folder"]:
                req.setdefault(dep, []).append(a)
    return req


def cmd_remove(args):
    root = get_addons_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    addons = installed_addons()
    folders = {a["folder"] for a in addons}
    needed = required_by(addons)
    for name in args.folders:  # check everything first so a bad name removes nothing
        # ponytail: removes only the folders named; multi-folder addons need each name listed
        if name not in folders:  # also rejects "..", paths and the backup folder
            sys.exit(f"Not an installed addon folder: {name}")
        # addons removed in the same command don't count as users
        users = [a["title"] for a in needed.get(name, []) if a["folder"] not in args.folders]
        if users and not args.force:
            sys.exit(f"{name} is required by {', '.join(users)}; use --force to remove it anyway")
    for name in args.folders:
        move_to_backup(root, name, stamp)
        print(f"removed {name} (backup: {BACKUP_DIR}/{stamp}/{name})")


# New every run. POSTs must send it, so another website can't drive the local server.
TOKEN = secrets.token_urlsafe(16)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESO Addon Manager</title>
<style>
:root {
  --ground: #11171a; --panel: #182025; --line: #2a353b;
  --text: #dcd6c6; --muted: #8e9b9c;
  --accent: #d2ac52; --accent-fill: #d2ac52; --accent-ink: #1a1405;
  --focus: #58a89e; --danger-fill: #d2634e; --danger-ink: #1b0d09;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  color-scheme: dark;
}
@media (prefers-color-scheme: light) {
  :root {
    --ground: #e9eeed; --panel: #f6f8f7; --line: #c3cfcd;
    --text: #1b2a2e; --muted: #56696c;
    --accent: #7a5a0e; --accent-fill: #d2ac52; --accent-ink: #1a1405;
    --focus: #1f7a70; --danger-fill: #b84a36; --danger-ink: #ffffff;
    color-scheme: light;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--text); font: 15px/1.45 var(--sans); }
main { max-width: 880px; margin: 0 auto; padding: 40px 20px 96px; }
h1 { font: 600 2.1rem/1.1 var(--serif); letter-spacing: -0.01em; margin: 0 0 6px; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

[role=tablist] { display: flex; gap: 24px; margin: 28px 0 20px; border-bottom: 1px solid var(--line); }
[role=tab] { background: none; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; padding: 10px 0;
  color: var(--muted); font: 500 1rem var(--sans); cursor: pointer; }
[role=tab][aria-selected=true] { color: var(--text); border-bottom-color: var(--accent); }
.count { margin-left: 4px; color: var(--muted); font-variant-numeric: tabular-nums; }

.btn { min-height: 34px; padding: 7px 12px; border: 1px solid var(--line); border-radius: 4px; background: transparent;
  color: var(--text); font: 500 .875rem var(--sans); cursor: pointer; }
.btn:hover:not(:disabled) { background: var(--panel); }
.btn:disabled { opacity: .55; cursor: default; }
.btn.primary { background: var(--accent-fill); border-color: transparent; color: var(--accent-ink); font-weight: 600; }
.btn.primary:hover:not(:disabled) { background: var(--accent-fill); filter: brightness(1.08); }
.btn.quiet { border-color: transparent; color: var(--muted); }
.btn.quiet:hover:not(:disabled) { color: var(--text); }
.btn.danger { background: var(--danger-fill); border-color: transparent; color: var(--danger-ink); font-weight: 600; }

.bar { position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between; gap: 16px;
  margin-bottom: 18px; padding: 14px 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 4px; }
.bar[hidden] { display: none; }
.fill { position: absolute; inset: 0 auto 0 0; width: 0; background: color-mix(in srgb, var(--accent) 26%, transparent);
  transition: width .35s ease; }
.bar > :not(.fill) { position: relative; }
.bar p { margin: 0; }
.bar .lead { font-size: 1.0625rem; }
.bar .sub { color: var(--muted); font-size: .8125rem; }

.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; margin-bottom: 8px; }
input[type=search] { flex: 1 1 220px; min-width: 0; padding: 8px 12px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 4px; color: var(--text); font: inherit; }
select { padding: 8px 12px; background: var(--panel); border: 1px solid var(--line); border-radius: 4px;
  color: var(--text); font: inherit; max-width: 100%; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 4px; overflow: hidden; }
.seg button { padding: 7px 12px; border: 0; background: none; color: var(--muted); font: inherit; cursor: pointer; }
.seg button[aria-pressed=true] { background: var(--panel); color: var(--text); }
.check { display: inline-flex; align-items: center; gap: 10px; margin-left: auto; color: var(--muted); font-size: .8125rem; }
.chk { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); cursor: pointer; }

.list { margin: 0; padding: 0; list-style: none; }
.row { display: grid; grid-template-columns: minmax(0, 1fr) 190px 180px; align-items: center; gap: 4px 16px;
  padding: 12px 0; border-bottom: 1px solid var(--line); }
.row.up { padding-left: 14px; box-shadow: inset 3px 0 0 var(--accent); }
.title { display: block; font: 500 1.0625rem/1.3 var(--serif); overflow-wrap: anywhere; }
a.title { width: fit-content; color: inherit; text-decoration: none; }
a.title:hover, a.title:focus-visible { color: var(--accent); text-decoration: underline; }
.folder, .note { display: block; color: var(--muted); font-size: .8125rem; overflow-wrap: anywhere; }
.vercell { font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.vercell .old { color: var(--muted); }
.vercell .arrow { margin: 0 6px; color: var(--muted); }
.vercell .new { color: var(--accent); font-weight: 600; }
.state { color: var(--focus); font-weight: 500; }
.actions { display: flex; justify-content: flex-end; gap: 8px; }
.name { grid-area: name; } .vercell { grid-area: ver; } .actions { grid-area: act; }
.row { grid-template-areas: "name ver act"; }
@media (max-width: 640px) {
  .row { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "name act" "ver ver"; }
}
.empty, .rhead { margin: 16px 0; color: var(--muted); }
.name { display: flex; align-items: flex-start; gap: 6px; }
.name > div { min-width: 0; }
.tog, .tog-pad { flex: none; width: 24px; height: 24px; margin-top: 1px; }
.tog { padding: 0; border: 0; border-radius: 4px; background: none; color: var(--muted); font: inherit; cursor: pointer; }
.tog:hover { color: var(--text); background: var(--panel); }
.tog::before { content: "▸"; display: inline-block; font-size: 1.125rem; line-height: 1; transition: transform .15s; }
.tog[aria-expanded=true]::before { transform: rotate(90deg); }
.row.has-deps { cursor: pointer; }
.row.has-deps:hover .tog { color: var(--text); }
.deps { padding: 0 0 8px; border-bottom: 1px solid var(--line); }
.deps[hidden] { display: none; }
.deps .list { margin-left: 41px; padding-left: 14px; border-left: 1px solid var(--line); }
.deps .row { padding: 8px 0; border-bottom: 0; }
.deps .title { font-size: .9375rem; }
.missing { color: var(--accent); font-weight: 600; }

#toast { position: fixed; left: 50%; bottom: 20px; max-width: min(560px, calc(100vw - 32px)); padding: 10px 16px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 4px; box-shadow: 0 4px 18px #0006;
  white-space: pre-line; cursor: pointer; opacity: 0; pointer-events: none;
  transform: translate(-50%, 8px); transition: opacity .2s, transform .2s; }
#toast.show { opacity: 1; pointer-events: auto; transform: translate(-50%, 0); }
#toast.bad { border-color: var(--danger-fill); }

dialog { width: calc(100vw - 32px); max-width: 440px; padding: 22px; background: var(--panel); color: var(--text);
  border: 1px solid var(--line); border-radius: 4px; }
dialog::backdrop { background: #0009; }
dialog h2 { margin: 0 0 10px; font: 600 1.25rem/1.3 var(--serif); }
dialog p { margin: 0 0 10px; }
dialog .warn { color: var(--accent); }
.dactions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<main>
  <h1>ESO Addon Manager</h1>

  <div role="tablist" id="tabs" aria-label="Views">
    <button role="tab" id="t-installed" aria-selected="true" aria-controls="p-installed">Installed<span class="count" id="n-installed"></span></button>
    <button role="tab" id="t-find" aria-selected="false" aria-controls="p-find">Find on ESOUI</button>
  </div>

  <section id="p-installed" role="tabpanel" aria-labelledby="t-installed">
    <div class="bar" id="bar" hidden>
      <div class="fill" id="fill"></div>
      <div>
        <p class="lead" id="bart"></p>
        <p class="sub" id="bars"></p>
      </div>
      <button class="btn primary" id="barb">Update all</button>
    </div>
    <div class="tools">
      <input type="search" id="flt" placeholder="Filter installed addons" aria-label="Filter installed addons">
      <div class="seg">
        <button id="f-all" aria-pressed="true">All</button>
        <button id="f-up" aria-pressed="false">Updates</button>
      </div>
      <label class="chk"><input type="checkbox" id="libs"> Show libraries <span id="n-libs"></span></label>
      <span class="check"><span id="ago"></span><button class="btn" id="chk">Check now</button></span>
    </div>
    <ul class="list" id="list"></ul>
    <p class="empty" id="empty" hidden></p>
  </section>

  <section id="p-find" role="tabpanel" aria-labelledby="t-find" hidden>
    <div class="tools">
      <input type="search" id="q" placeholder="Search by name, for example trade or lib async" aria-label="Search ESOUI">
      <select id="cat" aria-label="Category"><option value="">All categories</option></select>
    </div>
    <p class="rhead" id="rhead"></p>
    <ul class="list" id="res"></ul>
  </section>
</main>

<div id="toast" role="status" aria-live="polite"></div>

<dialog id="dlg">
  <form method="dialog">
    <h2 id="dh"></h2>
    <p id="db"></p>
    <p class="warn" id="dw" hidden></p>
    <div class="dactions">
      <button class="btn" value="cancel">Cancel</button>
      <button class="btn danger" id="dgo" value="go">Remove</button>
    </div>
  </form>
</dialog>

<script>
const T = "__TOKEN__";
const $ = id => document.getElementById(id);
const state = {rows: [], error: null, tab: "installed", only: false, libs: false, busy: false, searched: false, open: new Set()};
const canUpdate = r => r.outdated && r.local !== "?";

// Builds DOM with textContent only: names come from manifests and ESOUI and are never trusted as HTML.
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  e.append(...kids.flat().filter(k => k != null && k !== false));
  return e;
}

async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"X-Token": T, "Content-Type": "application/json"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error: "The local server sent an unexpected response."}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), {data: j});
  return j;
}

let toastTimer;
function notify(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  if (!bad) toastTimer = setTimeout(() => { t.className = ""; }, 6000);
}
$("toast").onclick = () => { $("toast").className = ""; };

async function busy(btn, label, fn) {
  btn.disabled = true;
  btn.textContent = label;
  await fn();
}

async function loadInstalled() {
  try {
    const d = await api("/api/installed");
    state.rows = d.rows;
    state.checked = d.checked;
    state.error = null;
  } catch (e) {
    state.rows = [];
    state.error = e.message;
  }
  renderInstalled();
}

async function refresh() {
  await loadInstalled();
  if (state.tab === "find") await runSearch();
}

async function install(uid) {
  try {
    const j = await api("/api/install", {id: uid});
    notify(j.log.trim() || "Installed");
  } catch (e) {
    notify(e.message, true);
  }
  await refresh();
}

function versionCell(r) {
  const c = h("div", {class: "vercell"});
  if (canUpdate(r)) {
    c.append(h("span", {class: "old"}, r.local), h("span", {class: "arrow", "aria-label": "to"}, "→"), h("span", {class: "new"}, r.remote));
  } else if (r.local === "?") {
    c.append("Version not readable", h("span", {class: "note"}, r.remote ? "ESOUI lists " + r.remote : "Not on ESOUI"));
  } else {
    c.append(r.local);
    if (!r.remote) c.append(h("span", {class: "note"}, "Not on ESOUI"));
  }
  return c;
}

// ESOUI page link when known, plain text otherwise. Only https links: a javascript: URL would run in this page.
const titleEl = (text, url) => url && url.startsWith("https://")
  ? h("a", {class: "title", href: url, target: "_blank", rel: "noopener noreferrer"}, text)
  : h("span", {class: "title"}, text);

// One level deep: a dependency's own dependencies show on its own row.
function depRow(folder) {
  const d = state.rows.find(x => x.folder === folder);
  return h("li", {class: "row"},
    h("div", {class: "name"}, h("div", {}, d ? titleEl(d.title, d.url) : h("span", {class: "title"}, folder),
      d && d.title.toLowerCase() !== folder.toLowerCase() ? h("span", {class: "folder"}, folder) : null)),
    d ? versionCell(d) : h("div", {class: "vercell"}),
    h("div", {class: "actions"}, d ? h("span", {class: "state"}, "Installed") : h("span", {class: "missing"}, "Not installed")));
}

function installedRow(r) {
  const up = canUpdate(r);
  const sameName = r.title.toLowerCase() === r.folder.toLowerCase();
  const open = state.open.has(r.folder);
  const deps = r.depends.length ? h("li", {class: "deps", id: "deps-" + r.folder, hidden: !open},
    h("ul", {class: "list", "aria-label": "Dependencies of " + r.title}, r.depends.map(depRow))) : null;
  const tog = deps ? h("button", {class: "tog", "aria-expanded": String(open), "aria-controls": deps.id,
    "aria-label": "Dependencies of " + r.title + " (" + r.depends.length + ")", onclick: e => {
      const now = !state.open.has(r.folder);
      now ? state.open.add(r.folder) : state.open.delete(r.folder);
      e.currentTarget.setAttribute("aria-expanded", String(now));
      deps.hidden = !now;
    }}) : h("span", {class: "tog-pad"});
  // The whole row toggles for the mouse; the arrow button stays the keyboard control.
  const rowClick = deps ? e => { if (!e.target.closest("a, button")) tog.click(); } : null;
  return [h("li", {class: "row" + (up ? " up" : "") + (deps ? " has-deps" : ""), onclick: rowClick},
    h("div", {class: "name"}, tog, h("div", {}, titleEl(r.title, r.url), sameName ? null : h("span", {class: "folder"}, r.folder),
      r.includes.length ? h("span", {class: "note"}, "Includes " + r.includes.join(", ")) : null)),
    versionCell(r),
    h("div", {class: "actions"},
      up ? h("button", {class: "btn primary", onclick: e => busy(e.currentTarget, "Updating…", () => install(r.uid))}, "Update") : null,
      h("button", {class: "btn quiet", onclick: () => askRemove(r)}, "Remove"))), deps];
}

function renderAgo() {
  if (!state.checked) return;
  const min = Math.floor((Date.now() / 1000 - state.checked) / 60);
  $("ago").textContent = min < 1 ? "Checked just now" : "Checked " + min + " min ago";
}

async function checkNow() {
  const b = $("chk");
  b.disabled = true;
  b.textContent = "Checking…";
  try {
    await api("/api/refresh", {});
    await refresh();
    const n = state.rows.filter(canUpdate).length;
    notify(n ? n + (n === 1 ? " update available." : " updates available.") : "Everything is up to date.");
  } catch (e) {
    notify(e.message, true);
    await refresh();
  }
  b.disabled = false;
  b.textContent = "Check now";
}

function renderInstalled() {
  renderAgo();
  const rows = state.rows, ups = rows.filter(canUpdate);
  $("n-installed").textContent = rows.length || "";
  const quietLibs = rows.filter(r => r.lib && !canUpdate(r)).length;
  $("n-libs").textContent = quietLibs ? "(" + quietLibs + ")" : "";
  $("f-up").textContent = "Updates" + (ups.length ? " (" + ups.length + ")" : "");
  if (!state.busy) {
    $("bar").hidden = ups.length === 0;
    $("bart").textContent = ups.length + (ups.length === 1 ? " update available" : " updates available");
    $("bars").textContent = "Old versions move to the backup folder, they are not deleted.";
    $("barb").disabled = false;
    $("fill").style.width = "0";
  }
  const q = $("flt").value.trim().toLowerCase();
  const shown = rows
    .filter(r => (!state.only || canUpdate(r)) && (state.libs || !r.lib || canUpdate(r)) &&
      (!q || (r.title + " " + r.folder + " " + r.includes.join(" ")).toLowerCase().includes(q)))
    .sort((a, b) => canUpdate(b) - canUpdate(a) || a.title.localeCompare(b.title));
  $("list").replaceChildren(...shown.flatMap(installedRow).filter(Boolean));
  const e = $("empty");
  e.hidden = shown.length > 0;
  e.textContent = state.error ? "Can't read the AddOns folder. " + state.error
    : !rows.length ? "No addons in this folder yet. Open Find on ESOUI to install one."
    : state.only && !q ? "Everything is up to date."
    : "No installed addon matches your filter.";
}

async function updateAll() {
  const todo = state.rows.filter(canUpdate), failed = [];
  state.busy = true;
  $("barb").disabled = true;
  for (let i = 0; i < todo.length; i++) {
    $("bart").textContent = "Updating " + todo[i].title;
    $("bars").textContent = (i + 1) + " of " + todo.length;
    $("fill").style.width = (i / todo.length * 100) + "%";
    try { await api("/api/install", {id: todo[i].uid}); }
    catch (e) { failed.push(todo[i].title + ": " + e.message); }
  }
  state.busy = false;
  await refresh();
  const ok = todo.length - failed.length;
  notify((ok ? "Updated " + ok + (ok === 1 ? " addon" : " addons") + ".\n" : "") + failed.join("\n"), failed.length > 0);
}

function askRemove(r) {
  const dlg = $("dlg"), users = r.required_by;
  $("dh").textContent = "Remove " + r.title + "?";
  $("db").textContent = "The folder " + r.folder + " moves to .eso-addons-backup inside your AddOns folder. Nothing is deleted.";
  $("dw").hidden = users.length === 0;
  $("dw").textContent = users.join(", ") + (users.length === 1 ? " needs" : " need") + " this addon and may stop loading without it.";
  $("dgo").textContent = users.length ? "Remove anyway" : "Remove";
  dlg.returnValue = "";
  dlg.addEventListener("close", async () => {
    if (dlg.returnValue !== "go") return;
    try {
      const j = await api("/api/remove", {folder: r.folder, force: true});
      notify(j.log.trim());
    } catch (e) {
      notify(e.message, true);
    }
    await refresh();
  }, {once: true});
  dlg.showModal();
}

let searchTimer;
async function runSearch() {
  state.searched = true;
  const q = $("q").value.trim(), cat = $("cat").value;
  try {
    const hits = await api("/api/search?q=" + encodeURIComponent(q) + "&cat=" + encodeURIComponent(cat));
    if (q !== $("q").value.trim() || cat !== $("cat").value) return;  // a newer search is already running
    const where = cat ? " in " + $("cat").selectedOptions[0].dataset.title : "";
    $("rhead").textContent = !hits.length ? "Nothing on ESOUI matches “" + q + "”" + where + ". Try fewer words or part of the folder name."
      : cat ? hits.length + (hits.length === 1 ? " addon" : " addons") + where + ", most downloaded first"
      : !q ? "Most downloaded this month"
      : hits.length === 30 ? "Top 30 matches by downloads"
      : hits.length + (hits.length === 1 ? " match" : " matches");
    $("res").replaceChildren(...hits.map(x => h("li", {class: "row"},
      h("div", {class: "name"}, titleEl(x.name, x.url)),
      h("div", {class: "vercell"}, x.version, h("span", {class: "note"}, Number(x.monthly).toLocaleString() + " downloads this month")),
      h("div", {class: "actions"}, x.installed
        ? h("span", {class: "state"}, "Installed")
        : h("button", {class: "btn primary", onclick: e => busy(e.currentTarget, "Installing…", () => install(x.uid))}, "Install")))));
  } catch (e) {
    notify(e.message, true);
  }
}

async function loadCategories() {
  try {
    const cats = await api("/api/categories");
    $("cat").append(...cats.map(c => h("option", {value: c.id, "data-title": c.title}, c.title + " (" + c.count + ")")));
  } catch (e) {
    notify(e.message, true);
  }
}

function showTab(name, focusSearch = true) {
  state.tab = name;
  for (const t of ["installed", "find"]) {
    $("t-" + t).setAttribute("aria-selected", t === name);
    $("p-" + t).hidden = t !== name;
  }
  if (name === "find") {
    if (!state.searched) { runSearch(); loadCategories(); }
    if (focusSearch) $("q").focus();
  }
}

function setOnly(v) {
  state.only = v;
  $("f-all").setAttribute("aria-pressed", !v);
  $("f-up").setAttribute("aria-pressed", v);
  renderInstalled();
}

$("t-installed").onclick = () => showTab("installed");
$("t-find").onclick = () => showTab("find");
$("tabs").onkeydown = e => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const next = state.tab === "installed" ? "find" : "installed";
  showTab(next, false);
  $("t-" + next).focus();
};
$("barb").onclick = updateAll;
$("chk").onclick = checkNow;
setInterval(renderAgo, 30000);
$("flt").oninput = renderInstalled;
$("libs").onchange = e => { state.libs = e.target.checked; renderInstalled(); };
$("f-all").onclick = () => setOnly(false);
$("f-up").onclick = () => setOnly(true);
$("cat").onchange = runSearch;
$("q").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); };
loadInstalled();
</script>
</body>
</html>
"""


def status_rows():
    """One row per installed addon: local vs ESOUI version, for the web page."""
    pairs, unmatched = match_installed()
    users = required_by(installed_addons())

    def row(a, f=None):
        return {
            "uid": f["UID"] if f else None,
            "folder": a["folder"],
            "title": a["title"],
            "local": a["version"],
            "remote": f["UIVersion"] if f else None,
            "outdated": bool(f) and is_outdated(a, f),
            "lib": a.get("lib", False) or a["folder"].lower().startswith("lib"),
            "url": f.get("UIFileInfoURL") if f else None,
            "includes": a["includes"],
            "depends": a["depends"],
            "required_by": sorted({u["title"] for u in users.get(a["folder"], [])}, key=str.lower),
        }

    rows = [row(a, f) for f, a in pairs] + [row(a) for a in unmatched]
    return sorted(rows, key=lambda r: r["title"].lower())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # no framing: a hidden iframe on another site could trick clicks on Remove/Install
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        # DNS rebinding guard: only answer requests addressed to localhost
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            self.reply(403, {"error": "bad host"})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/":
                self.reply(200, PAGE.replace("__TOKEN__", TOKEN), "text/html")
            elif u.path == "/api/installed":
                rows = status_rows()
                self.reply(200, {"rows": rows, "checked": CACHE_PATH.stat().st_mtime})
            elif u.path == "/api/search":
                root = get_addons_dir()
                qs = urllib.parse.parse_qs(u.query)
                q, cat = qs.get("q", [""])[0].split(), qs.get("cat", [""])[0]
                # No terms: the 30 most downloaded addons, a starting point for browsing. A category lists all of it.
                hits = [f for f in find(q) if f["UICATID"] == cat] if cat else find(q)[:30]
                self.reply(200, [
                    {"uid": f["UID"], "name": f["UIName"], "version": f["UIVersion"], "url": f.get("UIFileInfoURL"),
                     "monthly": f.get("UIDownloadMonthly") or "0",
                     "installed": any((root / d).is_dir() for d in f.get("UIDir") or [])}
                    for f in hits
                ])
            elif u.path == "/api/categories":
                n = {}
                for f in catalog():
                    n[f["UICATID"]] = n.get(f["UICATID"], 0) + 1
                names = categories()
                self.reply(200, sorted(
                    ({"id": i, "title": names.get(i, "Category " + i), "count": c} for i, c in n.items()),
                    key=lambda c: c["title"].lower()))
            else:
                self.reply(404, {"error": "not found"})
        except (OSError, SystemExit) as e:
            self.reply(500, {"error": str(e.code if isinstance(e, SystemExit) else e)})

    def do_POST(self):
        if not self.allowed():
            return
        if not secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self.reply(403, {"error": "bad token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                uid = str(body.get("id", ""))
                if self.path == "/api/refresh":
                    # a copy under a minute old is reused, so repeated clicks don't re-download several MB from ESOUI
                    catalog(max_age=60)
                    if catalog_age() >= 60:  # catalog() fell back to the old copy
                        return self.reply(502, {"error": "Could not reach ESOUI. Showing the last downloaded list."})
                elif self.path == "/api/install" and uid.isascii() and uid.isdigit():
                    cmd_install(argparse.Namespace(targets=[uid]))
                elif self.path == "/api/remove" and isinstance(body.get("folder"), str):
                    users = sorted(
                        {a["title"] for a in required_by(installed_addons()).get(body["folder"], [])},
                        key=str.lower,
                    )
                    if users and not body.get("force"):
                        return self.reply(409, {"error": f"Required by {', '.join(users)}", "required_by": users})
                    cmd_remove(argparse.Namespace(folders=[body["folder"]], force=True))
                else:
                    return self.reply(400, {"error": "bad request"})
            self.reply(200, {"log": out.getvalue()})
        except (OSError, ValueError, AttributeError, SystemExit) as e:
            self.reply(400, {"error": str(e.code if isinstance(e, SystemExit) else e)})


def make_server(port):
    return HTTPServer(("127.0.0.1", port), Handler)


# Unusual port, below the range the OS hands out for outgoing connections, so it stays the same between runs
DEFAULT_PORT = 23176


def cmd_web(args):
    try:
        srv = make_server(args.port or DEFAULT_PORT)
    except OSError:
        if args.port:  # a port the user asked for is never swapped silently
            sys.exit(f"Port {args.port} is in use; pick another with --port")
        srv = make_server(0)  # default taken: let the OS pick a free one
    print(f"http://127.0.0.1:{srv.server_port}/   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_config(args):
    cfg = load_config()
    if args.dir:
        p = Path(args.dir).expanduser()
        if not p.is_dir():
            sys.exit(f"Not a directory: {p}")
        cfg["addons_dir"] = str(p)
        save_config(cfg)
    print("AddOns folder:", get_addons_dir())


def cmd_list(args):
    addons = installed_addons()
    for a in addons:
        if args.libs_only and not a["folder"].lower().startswith("lib"):
            continue
        print(f"{a['folder']:<32} {a['version']:<14} {a['title']}")
    print(f"\n{len(addons)} addons/libraries", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(prog="eso_addons")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("config", help="show/set the AddOns folder")
    c.add_argument("--dir")
    c.set_defaults(fn=cmd_config)
    l = sub.add_parser("list", help="list installed addons")
    l.add_argument("--libs-only", action="store_true")
    l.set_defaults(fn=cmd_list)
    s = sub.add_parser("search", help="search the ESOUI catalog")
    s.add_argument("terms", nargs="+")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)
    o = sub.add_parser("outdated", help="compare installed versions with ESOUI")
    o.add_argument("--unmatched", action="store_true", help="also list folders not found on ESOUI")
    o.set_defaults(fn=cmd_outdated)
    i = sub.add_parser("install", help="install by ESOUI ID or folder name, plus missing dependencies")
    i.add_argument("targets", nargs="+")
    i.set_defaults(fn=cmd_install)
    r = sub.add_parser("remove", help="move addon folders to the backup directory")
    r.add_argument("folders", nargs="+")
    r.add_argument("--force", action="store_true", help="remove even if other addons depend on it")
    r.set_defaults(fn=cmd_remove)
    w = sub.add_parser("web", help="local web UI (127.0.0.1 only)")
    w.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}, or any free port if that is taken")
    w.set_defaults(fn=cmd_web)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()