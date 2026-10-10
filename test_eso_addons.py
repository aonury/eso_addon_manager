"""Run: python3 test_eso_addons.py. It prints a few install/remove lines; a traceback means a test failed."""
from eso_addons import ver_key

# same release written differently must compare equal
assert ver_key("1.0.47") == ver_key("1.0 r47")
assert ver_key("0.79") == ver_key("v0.79")
# real updates seen in the wild
assert ver_key("4.6") < ver_key("4.6.18")
assert ver_key("1.1 r37") < ver_key("1.1 r39")
assert ver_key("69") < ver_key("78")
assert ver_key("1.99.2") < ver_key("1.100.22")  # numeric, not string, order
# unknown local version sorts below everything, so it gets listed
assert ver_key("?") < ver_key("3.16.12")
# local newer than ESOUI is not outdated
assert not ver_key("2.0 r44") < ver_key("2.0 r43")

# --- install / remove with a fake network ---
import argparse
import hashlib
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import eso_addons as m


def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return b.getvalue()


def routes_for(zips):
    """zips: {uid: zip bytes} -> {url: response bytes}"""
    r = {}
    for uid, data in zips.items():
        r[f"{m.API}/filedetails/{uid}.json"] = json.dumps(
            [{"UIName": f"A{uid}", "UIVersion": "1", "UIDownload": f"https://x/{uid}.zip",
              "UIMD5": hashlib.md5(data).hexdigest()}]
        ).encode()
        r[f"https://x/{uid}.zip"] = data
    return r


def run_install(root, routes, uid, by_dir):
    fake = lambda req, timeout=None: io.BytesIO(routes[req.full_url])
    with mock.patch("urllib.request.urlopen", fake):
        m.install_one(uid, root, "T1", by_dir, set())


by_dir = {"Foo": {"UID": "10"}, "LibBar": {"UID": "20"}}
foo = make_zip({"Foo/Foo.txt": "## Title: Foo\n## DependsOn: LibBar>=1\n", "Foo/a.lua": "x"})
bar = make_zip({"LibBar/LibBar.txt": "## Title: LibBar\n"})

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "AddOns"
    root.mkdir()
    # fresh install pulls in the missing dependency
    run_install(root, routes_for({"10": foo, "20": bar}), "10", by_dir)
    assert (root / "Foo" / "a.lua").is_file() and (root / "LibBar" / "LibBar.txt").is_file()
    # zip manifest has no/old ## Version (Display Leads 42.1 ships "42.0"): still up to date after install
    assert not m.is_outdated(m.addon_meta(root / "Foo"), {"UIVersion": "1"})
    assert m.is_outdated(m.addon_meta(root / "Foo"), {"UIVersion": "2"})
    # reinstall moves the old folder to the backup instead of overwriting it
    (root / "Foo" / "old.txt").write_text("keep me")
    run_install(root, routes_for({"10": foo, "20": bar}), "10", by_dir)
    assert not (root / "Foo" / "old.txt").exists()
    assert (root / m.BACKUP_DIR / "T1" / "Foo" / "old.txt").read_text() == "keep me"
    # Foo needs LibBar, so remove refuses unless forced; a refused remove touches nothing
    with mock.patch.object(m, "get_addons_dir", return_value=root):
        try:
            m.cmd_remove(argparse.Namespace(folders=["LibBar"], force=False))
            raise AssertionError("removed a library that is still needed")
        except SystemExit as e:
            assert "Foo" in str(e.code)
        assert (root / "LibBar").is_dir()
        # removing both together is fine: Foo goes too, so nobody is left needing LibBar
        m.cmd_remove(argparse.Namespace(folders=["LibBar", "Foo"], force=False))
    assert not (root / "LibBar").exists() and not (root / "Foo").exists()
    assert list((root / m.BACKUP_DIR).glob("*/LibBar"))

    # zip path escape is refused and writes nothing outside AddOns
    evil = make_zip({"../evil.txt": "x"})
    try:
        run_install(root, routes_for({"30": evil}), "30", by_dir)
        raise AssertionError("evil zip was accepted")
    except SystemExit:
        pass
    assert not (Path(tmp) / "evil.txt").exists()

    # corrupted download (md5 mismatch) is refused
    bad = routes_for({"40": bar})
    bad["https://x/40.zip"] = bar + b"junk"
    try:
        run_install(root, bad, "40", by_dir)
        raise AssertionError("corrupt zip was accepted")
    except SystemExit:
        pass

    # a zip must not write into the backup folder
    try:
        run_install(root, routes_for({"50": make_zip({m.BACKUP_DIR + "/x.txt": "x"})}), "50", by_dir)
        raise AssertionError("zip touching the backup folder was accepted")
    except SystemExit:
        pass
    # remove takes installed addon folder names only, never a path, ".." or the backup folder
    (root / "Keep").mkdir()
    (root / "Keep" / "Keep.txt").write_text("## Title: Keep\n")
    with mock.patch.object(m, "get_addons_dir", return_value=root):
        for name in ("..", ".", m.BACKUP_DIR, "Keep/..", str(root / "Keep")):
            try:
                m.cmd_remove(argparse.Namespace(folders=[name], force=True))
                raise AssertionError(f"remove accepted {name!r}")
            except SystemExit:
                pass
    assert (root / "Keep").is_dir() and root.is_dir()

# downloads are https only: urllib would also read file:// URLs
try:
    m.http_get("file:///etc/passwd")
    raise AssertionError("file:// URL was fetched")
except ValueError:
    pass

# a broken catalog download keeps the old cache instead of replacing it
with tempfile.TemporaryDirectory() as tmp:
    c = Path(tmp) / "filelist.json"
    c.write_text('[{"UID": "1"}]')
    os.utime(c, (0, 0))  # stale, so catalog() tries to download
    with mock.patch.object(m, "CACHE_PATH", c), mock.patch.object(m, "http_get", return_value=b"<html>oops"):
        assert m.catalog() == [{"UID": "1"}] and c.read_text() == '[{"UID": "1"}]'

# --- web server: writes need the token and a localhost Host header ---
import http.client
import threading

cat = [{"UID": "10", "UICATID": "53", "UIName": "Foo", "UIVersion": "2", "UIDir": ["Foo"], "UIDownloadMonthly": "5",
        "UIFileInfoURL": "https://esoui/10"},
       {"UID": "11", "UICATID": "27", "UIName": "Other", "UIVersion": "1", "UIDir": ["Other"], "UIDownloadMonthly": "9"}]
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "Foo").mkdir()
    (root / "Foo" / "Foo.txt").write_text("## Title: Foo\n## Version: 1\n")
    (root / "Bar").mkdir()
    (root / "Bar" / "Bar.txt").write_text("## Title: Bar\n## IsLibrary: true\n## DependsOn: Foo\n")
    # HarvestMap layout: stub manifest on top, real addon and extras nested; the nested one needs Foo
    (root / "Hm" / "Modules" / "Hm").mkdir(parents=True)
    (root / "Hm" / "Hm.txt").write_text("## Title: Hm\n")
    (root / "Hm" / "Modules" / "Hm" / "Hm.addon").write_text("## Title: Hm\n## Version: 3.1\n## DependsOn: Foo HmData\n")
    (root / "Hm" / "HmData").mkdir()
    (root / "Hm" / "HmData" / "HmData.txt").write_text("## Title: Hm Data\n")
    cache = root.parent / "filelist.json"
    cache.write_text("[]")
    with mock.patch.object(m, "get_addons_dir", return_value=root), mock.patch.object(m, "catalog", return_value=cat), \
            mock.patch.object(m, "categories", return_value={"53": "Libraries"}), mock.patch.object(m, "CACHE_PATH", cache):
        srv = m.make_server(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def call(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_port)
            c.request(method, path, json.dumps(body) if body is not None else None, headers or {})
            r = c.getresponse()
            call.headers = r.headers
            return r.status, json.loads(r.read())

        status, data = call("GET", "/api/installed")
        by = {r["folder"]: r for r in data["rows"]}
        assert status == 200 and data["checked"] == cache.stat().st_mtime
        assert by["Foo"]["outdated"] is True and by["Foo"]["uid"] == "10" and by["Foo"]["required_by"] == ["Bar", "Hm"]
        assert by["Foo"]["url"] == "https://esoui/10" and by["Bar"]["url"] is None
        # nested manifests fold into the top folder; HmData ships inside Hm, so it is not a dependency
        assert by["Hm"]["depends"] == ["Foo"] and by["Hm"]["local"] == "3.1" and by["Hm"]["includes"] == ["Hm Data"]
        assert by["Bar"]["lib"] is True and by["Bar"]["uid"] is None  # IsLibrary flag read from the manifest
        hit = call("GET", "/api/search?q=fo")[1][0]
        assert hit["name"] == "Foo" and hit["installed"] is True
        assert call("GET", "/api/search")[1][0]["name"] == "Other"  # no terms = most downloaded
        # a category lists only its own addons; unknown category ids still get a name
        assert [x["name"] for x in call("GET", "/api/search?cat=53")[1]] == ["Foo"]
        assert call("GET", "/api/categories")[1] == [
            {"id": "27", "title": "Category 27", "count": 1}, {"id": "53", "title": "Libraries", "count": 1}]
        # the page can't be framed by another site (clickjacking)
        call("GET", "/api/installed")
        assert call.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in call.headers["Content-Security-Policy"]
        # a page on another site must not be able to change files
        tok = {"X-Token": m.TOKEN}
        assert call("POST", "/api/remove", {"folder": "Foo", "force": True})[0] == 403
        assert call("POST", "/api/remove", {"folder": "Foo", "force": True}, {**tok, "Host": "evil.example"})[0] == 403
        assert (root / "Foo").is_dir()
        assert call("POST", "/api/remove", {"folder": "..", "force": True}, tok)[0] == 400
        # Check now: needs the token; an old catalog after the refresh means ESOUI was unreachable, and the page says so
        assert call("POST", "/api/refresh", {})[0] == 403
        assert call("POST", "/api/refresh", {}, tok)[0] == 200
        os.utime(cache, (0, 0))
        status, body = call("POST", "/api/refresh", {}, tok)
        assert status == 502 and "Could not reach ESOUI" in body["error"]
        assert call("POST", "/api/install", {"id": "²"}, tok)[0] == 400  # str.isdigit() alone accepts this
        # Bar needs Foo: refused with the list of users, unless forced
        status, body = call("POST", "/api/remove", {"folder": "Foo"}, tok)
        assert status == 409 and body["required_by"] == ["Bar", "Hm"] and (root / "Foo").is_dir()
        assert call("POST", "/api/remove", {"folder": "Foo", "force": True}, tok)[0] == 200
        assert not (root / "Foo").exists()
        srv.shutdown()