#!/usr/bin/env python3
"""Debrid Fetch: browse a TorBox / Real-Debrid library and pull files to a local folder.

Standard library only. Downloads run through aria2 (several connections per
file); a static aria2c build is fetched on first start when none is installed,
and a built-in single-connection downloader is used if that fails.

Configuration through environment variables:
  PORT           port to listen on (default 8765)
  HOST           address to bind (default 0.0.0.0)
  DOWNLOAD_ROOT  folder downloads are written under and that Local files shows
  ROOT_LABEL     name shown for that folder (default: its folder name)
  DATA_DIR       where config.json, jobs.json, manifest.json and bin/aria2c live
  ARIA2C         path to an aria2c binary to use instead of the fetched one
"""
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")
ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads")).resolve()
# Name shown for the download folder in the page.
ROOT_LABEL = os.environ.get("ROOT_LABEL") or ROOT.name or "/"
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data")).resolve()
CONFIG_FILE = DATA_DIR / "config.json"
JOBS_FILE = DATA_DIR / "jobs.json"
MANIFEST_FILE = DATA_DIR / "manifest.json"

USER_AGENT = "debrid-fetch/2.0"
TORBOX_API = os.environ.get("TORBOX_API", "https://api.torbox.app/v1/api")
RD_API = os.environ.get("RD_API", "https://api.real-debrid.com/rest/1.0")
CHUNK = 1 << 20
MAX_ATTEMPTS = 6
LIST_CACHE_SECONDS = 60
SCAN_CACHE_SECONDS = 20

# Static aria2c builds, pinned by checksum.
ARIA2_BUILDS = {
    "x86_64": ("https://github.com/abcfy2/aria2-static-build/releases/download/1.37.0/"
               "aria2-x86_64-linux-musl_static.zip",
               "e0a09b12ef67f35f8a8e4fdddbec851d235b7c31da549d0578bff459032b499a"),
    "aarch64": ("https://github.com/abcfy2/aria2-static-build/releases/download/1.37.0/"
                "aria2-aarch64-linux-musl_static.zip",
                "0c681a89a40e0f82d1f5137608e86257eb0af201459c002941ea098f2b8c26b6"),
}


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- config

config_lock = threading.Lock()
config = {"torbox_key": "", "rd_key": "", "concurrency": 2, "connections": 8}


def load_config():
    try:
        config.update(json.loads(CONFIG_FILE.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Could not read {CONFIG_FILE}: {e}", file=sys.stderr)


def save_config():
    write_private(CONFIG_FILE, json.dumps(config, indent=2))


def write_private(path, text):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- http client

def http_json(url, token=None, data=None, timeout=45):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err = json.loads(e.read())
            detail = err.get("detail") or err.get("error") or ""
        except Exception:
            pass
        raise ApiError(f"{detail or e.reason} (HTTP {e.code})") from None
    except urllib.error.URLError as e:
        raise ApiError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None


# ---------------------------------------------------------------- paths

BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')


def clean_relpath(path):
    """Turn a provider-supplied path into safe relative path components."""
    parts = []
    for part in re.split(r"[/\\]+", str(path)):
        part = BAD_CHARS.sub("_", part).strip().rstrip(".")
        if part in ("", ".", ".."):
            continue
        while len(part.encode()) > 200:
            stem, dot, ext = part.rpartition(".")
            part = (stem[:-1] + dot + ext) if dot and len(ext) < 10 and stem else part[:-1]
        parts.append(part)
    return parts


def resolve_under_root(rel):
    target = ROOT.joinpath(*clean_relpath(rel)).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ApiError("Folder is outside the download folder")
    return target


def local_entry(rel):
    """Path of an existing entry under the root, without following a final symlink."""
    parts = clean_relpath(rel)
    if not parts:
        raise ApiError("The download folder itself cannot be changed")
    parent = resolve_under_root("/".join(parts[:-1]))
    return parent / parts[-1]


def rel_of(path):
    return path.relative_to(ROOT).as_posix()


# ---------------------------------------------------------------- providers

class TorBox:
    label = "TorBox"
    KINDS = {"torrents": "torrent_id", "usenet": "usenet_id", "webdl": "web_id"}

    def key(self):
        if not config["torbox_key"]:
            raise ApiError("No TorBox API key set. Add it under Settings.")
        return config["torbox_key"]

    def configured(self):
        return bool(config["torbox_key"])

    def check(self, key):
        me = http_json(f"{TORBOX_API}/user/me", token=key)
        return (me.get("data") or {}).get("email", "")

    def kind_of(self, item_id):
        return item_id.partition(":")[0]

    def items(self, fresh):
        """Returns the items and the kinds that could be listed."""
        out, loaded = [], set()
        for kind in self.KINDS:
            try:
                rows = self._mylist(kind, fresh)
            except ApiError:
                # Usenet and web downloads are not part of every plan.
                if kind == "torrents":
                    raise
                continue
            loaded.add(kind)
            for row in rows:
                files = [
                    {"id": str(f.get("id")), "path": f.get("name") or f.get("short_name") or str(f.get("id")),
                     "size": f.get("size") or 0}
                    for f in row.get("files") or []
                ]
                out.append({
                    "id": f"{kind}:{row.get('id')}",
                    "kind": kind,
                    "name": row.get("name") or "(unnamed)",
                    "size": row.get("size") or 0,
                    "added": row.get("created_at") or "",
                    "ready": bool(row.get("download_finished") and row.get("download_present")),
                    "state": row.get("download_state") or "",
                    "files": files,
                })
        return out, loaded

    def _mylist(self, kind, fresh):
        rows, offset, limit = [], 0, 1000
        while True:
            q = {"offset": offset, "limit": limit}
            if fresh:
                q["bypass_cache"] = "true"
            res = http_json(f"{TORBOX_API}/{kind}/mylist?{urllib.parse.urlencode(q)}", token=self.key())
            page = (res or {}).get("data") or []
            if isinstance(page, dict):
                page = [page]
            rows += page
            if len(page) < limit:
                return rows
            offset += limit

    def files(self, item_id):
        raise ApiError("TorBox lists files with the item")

    def link(self, item_id, file_id):
        kind, _, num = item_id.partition(":")
        if kind not in self.KINDS:
            raise ApiError("Unknown TorBox item")
        q = {"token": self.key(), self.KINDS[kind]: num, "file_id": file_id, "redirect": "false"}
        res = http_json(f"{TORBOX_API}/{kind}/requestdl?{urllib.parse.urlencode(q)}")
        url = (res or {}).get("data")
        if not url:
            raise ApiError((res or {}).get("detail") or "TorBox returned no download link")
        return url


class RealDebrid:
    label = "Real-Debrid"

    def key(self):
        if not config["rd_key"]:
            raise ApiError("No Real-Debrid API token set. Add it under Settings.")
        return config["rd_key"]

    def configured(self):
        return bool(config["rd_key"])

    def check(self, key):
        return (http_json(f"{RD_API}/user", token=key) or {}).get("username", "")

    def kind_of(self, item_id):
        return "torrents"

    def items(self, fresh):
        out, page = [], 1
        while True:
            rows = http_json(f"{RD_API}/torrents?limit=500&page={page}", token=self.key()) or []
            for row in rows:
                out.append({
                    "id": str(row.get("id")),
                    "kind": "torrents",
                    "name": row.get("filename") or "(unnamed)",
                    "size": row.get("bytes") or 0,
                    "added": row.get("added") or "",
                    "ready": row.get("status") == "downloaded",
                    "state": row.get("status") or "",
                    "files": None,
                })
            if len(rows) < 500:
                return out, {"torrents"}
            page += 1

    def _info(self, item_id):
        return http_json(f"{RD_API}/torrents/info/{urllib.parse.quote(item_id, safe='')}", token=self.key()) or {}

    def files(self, item_id):
        info = self._info(item_id)
        links = info.get("links") or []
        selected = [f for f in info.get("files") or [] if f.get("selected")]
        name = info.get("filename") or item_id
        if len(selected) == len(links):
            prefix = f"{name}/" if len(selected) > 1 else ""
            return [
                {"id": str(i), "path": prefix + (f.get("path") or "").lstrip("/"), "size": f.get("bytes") or 0}
                for i, f in enumerate(selected)
            ]
        # Real-Debrid packed the torrent into fewer links than files (e.g. one archive).
        if len(links) == 1:
            return [{"id": "0", "path": name, "size": info.get("bytes") or 0, "rename": True}]
        return [{"id": str(i), "path": f"{name}/part {i + 1}", "size": 0, "rename": True} for i in range(len(links))]

    def link(self, item_id, file_id):
        links = self._info(item_id).get("links") or []
        try:
            hoster = links[int(file_id)]
        except (ValueError, IndexError):
            raise ApiError("File is no longer available on Real-Debrid") from None
        res = http_json(f"{RD_API}/unrestrict/link", token=self.key(), data={"link": hoster}) or {}
        if not res.get("download"):
            raise ApiError("Real-Debrid returned no download link")
        return res["download"], res.get("filename")


PROVIDERS = {"torbox": TorBox(), "realdebrid": RealDebrid()}
list_cache = {}


def get_provider(name):
    if name not in PROVIDERS:
        raise ApiError("Unknown provider")
    return PROVIDERS[name]


def list_items(name, fresh):
    """Returns (items, kinds that were listed) for a provider, cached briefly."""
    cached = list_cache.get(name)
    if cached and not fresh and time.time() - cached[0] < LIST_CACHE_SECONDS:
        return cached[1], cached[2]
    items, kinds = get_provider(name).items(fresh)
    items.sort(key=lambda i: i["added"], reverse=True)
    list_cache[name] = (time.time(), items, kinds)
    return items, kinds


# ---------------------------------------------------------------- local files

class Local:
    """Index of the files under the download folder, and where each came from.

    The manifest maps a relative path to the debrid item it belongs to. Files
    are added when this app downloads them, or when a file already on disk
    matches a debrid file by name and size.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.manifest = {}
        self.files = None
        self.scanned = 0

    def load(self):
        try:
            self.manifest = json.loads(MANIFEST_FILE.read_text())
        except Exception:
            self.manifest = {}

    def save(self):
        with self.lock:
            text = json.dumps(self.manifest)
        try:
            write_private(MANIFEST_FILE, text)
        except OSError as e:
            print(f"Could not save manifest: {e}", file=sys.stderr)

    def invalidate(self):
        self.scanned = 0

    def scan(self, force=False):
        """Returns {relative path: (size, mtime)}, or None when the folder is unavailable."""
        with self.lock:
            if not force and self.files is not None and time.time() - self.scanned < SCAN_CACHE_SECONDS:
                return self.files
        if not ROOT.is_dir():
            return None
        files = {}
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith(".") or name.endswith((".part", ".aria2")):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                files[Path(full).relative_to(ROOT).as_posix()] = (st.st_size, st.st_mtime)
        with self.lock:
            self.files, self.scanned = files, time.time()
            # An empty folder with a full manifest looks like a missing drive, so keep the manifest.
            if files:
                stale = [rel for rel in self.manifest if rel not in files]
                for rel in stale:
                    del self.manifest[rel]
                if stale:
                    self.save()
        return files

    def record(self, rel, provider, item_id, file_id, item_name, size):
        with self.lock:
            self.manifest[rel] = {"provider": provider, "item_id": item_id, "file_id": file_id,
                                  "item_name": item_name, "size": size}
            self.invalidate()
        self.save()

    def forget(self, rel):
        with self.lock:
            for key in [k for k in self.manifest if k == rel or k.startswith(rel + "/")]:
                del self.manifest[key]
            self.invalidate()
        self.save()

    def remap(self, old, new):
        with self.lock:
            for key in [k for k in self.manifest if k == old or k.startswith(old + "/")]:
                self.manifest[new + key[len(old):]] = self.manifest.pop(key)
            self.invalidate()
        self.save()

    def annotate(self, provider, items):
        """Copies of the items with, per file, the local path it is stored at (or '')."""
        files = self.scan() or {}
        by_name = {}
        for rel, (size, _) in files.items():
            by_name.setdefault((rel.rpartition("/")[2], size), rel)
        out, changed = [], False
        with self.lock:
            by_key, per_item = {}, {}
            for rel, e in self.manifest.items():
                if e["provider"] == provider and rel in files:
                    by_key[(e["item_id"], e["file_id"])] = rel
                    per_item[e["item_id"]] = per_item.get(e["item_id"], 0) + 1
            for item in items:
                item = dict(item)
                if item["files"] is None:
                    item["local_files"] = per_item.get(item["id"], 0)
                else:
                    original, item["files"], count = item["files"], [], 0
                    for f in original:
                        f = dict(f)
                        rel = by_key.get((item["id"], f["id"]))
                        if rel is None and f.get("size"):
                            name = (clean_relpath(f["path"]) or [""])[-1]
                            rel = by_name.get((name, f["size"]))
                            known = self.manifest.get(rel)
                            if rel and (known is None or known["provider"] == provider):
                                self.manifest[rel] = {"provider": provider, "item_id": item["id"],
                                                      "file_id": f["id"], "item_name": item["name"],
                                                      "size": f["size"]}
                                changed = True
                        f["local"] = rel or ""
                        count += bool(rel)
                        item["files"].append(f)
                    item["local_files"] = count
                out.append(item)
        if changed:
            self.save()
        return out


local = Local()


def orphans(fresh):
    """Local files whose debrid item no longer exists at the service they came from."""
    files = local.scan(force=True)
    notes, loaded, present = [], {}, set()
    for name, provider in PROVIDERS.items():
        if not provider.configured():
            continue
        try:
            items, kinds = list_items(name, fresh)
        except ApiError as e:
            notes.append(f"Could not check {provider.label}: {e}")
            continue
        loaded[name] = ({i["id"] for i in items}, kinds)
        for item in items:
            for f in item["files"] or []:
                present.add(((clean_relpath(f["path"]) or [""])[-1], f["size"]))
    found = []
    if files is None:
        return {"files": [], "notes": [f"Download folder {ROOT} is not available"]}
    with local.lock:
        entries = list(local.manifest.items())
    for rel, e in entries:
        if rel not in files or e["provider"] not in loaded:
            continue
        ids, kinds = loaded[e["provider"]]
        if PROVIDERS[e["provider"]].kind_of(e["item_id"]) not in kinds or e["item_id"] in ids:
            continue
        size, mtime = files[rel]
        # Still offered by a debrid service under another item: not an orphan.
        if (rel.rpartition("/")[2], size) in present:
            continue
        found.append({"path": rel, "size": size, "mtime": mtime, "provider": e["provider"],
                      "item_name": e.get("item_name") or ""})
    found.sort(key=lambda f: f["path"].lower())
    return {"files": found, "notes": notes}


def local_list(rel):
    here = resolve_under_root(rel)
    if not here.is_dir():
        here = ROOT
    if not here.is_dir():
        raise ApiError(f"Download folder {ROOT} is not available")
    files = local.scan() or {}
    base = "" if here == ROOT else rel_of(here)
    prefix = base + "/" if base else ""
    with local.lock:
        sources = {k: v["provider"] for k, v in local.manifest.items()}
    entries = []
    for entry in os.scandir(here):
        if entry.name.startswith(".") or entry.name.endswith(".aria2"):
            continue
        path = prefix + entry.name
        try:
            st = entry.stat()
        except OSError:
            continue
        if entry.is_dir():
            inside = [v[0] for k, v in files.items() if k.startswith(path + "/")]
            entries.append({"name": entry.name, "dir": True, "size": sum(inside), "count": len(inside),
                            "mtime": st.st_mtime})
        else:
            entries.append({"name": entry.name, "dir": False, "size": st.st_size, "mtime": st.st_mtime,
                            "partial": entry.name.endswith(".part"), "source": sources.get(path, "")})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": base, "entries": entries}


def local_delete(paths, prune):
    with jobs_lock:
        active = [j["rel"] for j in jobs if j["status"] == "downloading"]
    deleted = 0
    for rel in paths:
        path = local_entry(rel)
        rel = rel_of(path)
        if any(a == rel or a.startswith(rel + "/") or a + ".part" == rel for a in active):
            raise ApiError(f"{path.name} is still downloading")
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        local.forget(rel)
        if prune:
            # Remove folders this left empty, but keep the top-level ones.
            parent = path.parent
            while parent != ROOT and parent.parent != ROOT:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    return deleted


def local_rename(rel, name):
    path = local_entry(rel)
    new = clean_relpath(name)
    if len(new) != 1 or "/" in name or "\\" in name:
        raise ApiError("Enter a single name without slashes")
    target = path.with_name(new[0])
    if not os.path.lexists(path):
        raise ApiError("That file no longer exists")
    if os.path.lexists(target):
        raise ApiError(f"{new[0]} already exists")
    os.rename(path, target)
    local.remap(rel_of(path), rel_of(target))


def local_move(paths, dest):
    folder = resolve_under_root(dest)
    if not folder.is_dir():
        raise ApiError("Target folder does not exist")
    for rel in paths:
        path = local_entry(rel)
        target = folder / path.name
        if path.parent == folder:
            continue
        if folder == path or path in folder.parents:
            raise ApiError(f"Cannot move {path.name} into itself")
        if os.path.lexists(target):
            raise ApiError(f"{path.name} already exists in the target folder")
        shutil.move(str(path), str(target))
        local.remap(rel_of(path), rel_of(target))


# ---------------------------------------------------------------- aria2

class Aria2:
    def __init__(self):
        self.lock = threading.Lock()
        self.binary = None
        self.version = ""
        self.error = "starting"
        self.proc = None
        self.port = 0
        self.secret = ""
        self.failed_at = 0

    def _locate(self):
        candidate = os.environ.get("ARIA2C") or shutil.which("aria2c")
        if not candidate:
            fetched = DATA_DIR / "bin" / "aria2c"
            if not fetched.exists():
                self._fetch(fetched)
            candidate = str(fetched)
        try:
            self._probe(candidate)
        except OSError:
            # The data folder may be on a drive that does not allow running programs.
            copy = Path(tempfile.gettempdir()) / "debrid-fetch-aria2c"
            shutil.copyfile(candidate, copy)
            copy.chmod(0o755)
            candidate = str(copy)
            self._probe(candidate)
        self.binary = candidate

    def _probe(self, binary):
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15).stdout
        self.version = (re.search(r"aria2 version (\S+)", out) or [None, "?"])[1]

    def _fetch(self, target):
        machine = platform.machine()
        if machine not in ARIA2_BUILDS:
            raise ApiError(f"no aria2 build for {machine}")
        url, digest = ARIA2_BUILDS[machine]
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ApiError("downloaded aria2 build failed its checksum")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name("aria2c.tmp")
        tmp.write_bytes(zipfile.ZipFile(io.BytesIO(data)).read("aria2c"))
        tmp.chmod(0o755)
        os.replace(tmp, target)

    def start(self):
        """Makes sure aria2 is running. Returns False when it cannot be used."""
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return True
            # After a failure, use the built-in downloader for a while before trying again.
            if time.time() - self.failed_at < 300:
                return False
            try:
                if not self.binary:
                    self._locate()
                self._spawn()
                self.error = ""
                return True
            except Exception as e:
                self.error = str(e) or type(e).__name__
                self.failed_at = time.time()
                self.proc = None
                return False

    def _spawn(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.secret = uuid.uuid4().hex
        args = [
            self.binary, "--no-conf=true", "--enable-rpc=true", "--rpc-listen-all=false",
            f"--rpc-listen-port={self.port}", f"--rpc-secret={self.secret}", f"--stop-with-process={os.getpid()}",
            "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=true", "--file-allocation=none",
            "--max-concurrent-downloads=16", "--max-tries=3", "--retry-wait=3", "--timeout=60",
            "--connect-timeout=30", "--summary-interval=0", "--console-log-level=error", "--enable-dht=false",
            "--follow-torrent=false", "--follow-metalink=false", f"--user-agent={USER_AGENT}",
        ]
        for ca in (os.environ.get("SSL_CERT_FILE"), "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"):
            if ca and os.path.exists(ca):
                args.append(f"--ca-certificate={ca}")
                break
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     env={**os.environ, "HOME": tempfile.gettempdir()})
        for _ in range(50):
            try:
                self.call("getVersion")
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise ApiError(f"aria2 exited with code {self.proc.returncode}") from None
                time.sleep(0.2)
        raise ApiError("aria2 did not start")

    def call(self, method, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": f"aria2.{method}",
                           "params": [f"token:{self.secret}", *params]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/jsonrpc", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read())["error"]["message"]
            except Exception:
                message = e.reason
            raise OSError(f"aria2: {message}") from None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


aria2 = Aria2()


# ---------------------------------------------------------------- download queue

jobs = []
jobs_lock = threading.Condition()
JOB_FIELDS = ("id", "provider", "item_id", "item_name", "file_id", "name", "rel", "size",
              "done", "status", "error", "rename", "added")


class Cancelled(Exception):
    pass


def save_jobs():
    with jobs_lock:
        data = [{k: j.get(k) for k in JOB_FIELDS} for j in jobs]
    try:
        write_private(JOBS_FILE, json.dumps(data))
    except OSError as e:
        print(f"Could not save queue: {e}", file=sys.stderr)


def load_jobs():
    try:
        data = json.loads(JOBS_FILE.read_text())
    except Exception:
        return
    for j in data:
        if "rel" not in j:
            # Queue saved by the first version, which stored absolute paths.
            try:
                j["rel"] = Path(j.pop("target")).relative_to(ROOT).as_posix()
            except (KeyError, ValueError):
                continue
        if j.get("status") == "downloading":
            j["status"] = "queued"
        jobs.append(j)


def public_job(j):
    return {"id": j["id"], "provider": j["provider"], "item_name": j["item_name"], "name": j["name"],
            "path": j["rel"], "size": j.get("size") or 0, "done": j.get("done") or 0,
            "speed": j.get("speed") or 0, "status": j["status"], "error": j.get("error") or ""}


def enqueue(provider, dest, items):
    get_provider(provider)
    base = clean_relpath(rel_of(resolve_under_root(dest)))
    added = 0
    with jobs_lock:
        busy = {j["rel"] for j in jobs if j["status"] in ("queued", "downloading")}
        for item in items:
            for f in item.get("files") or []:
                parts = clean_relpath(f.get("path") or "")
                if not parts:
                    continue
                rel = "/".join(base + parts)
                if rel in busy:
                    continue
                busy.add(rel)
                jobs.append({
                    "id": uuid.uuid4().hex[:12], "provider": provider, "item_id": str(item.get("id")),
                    "item_name": str(item.get("name") or ""), "file_id": str(f.get("id")),
                    "name": parts[-1], "rel": rel, "size": int(f.get("size") or 0), "done": 0,
                    "status": "queued", "error": "", "rename": bool(f.get("rename")), "added": time.time(),
                })
                added += 1
        jobs_lock.notify_all()
    save_jobs()
    return added


def worker(index):
    while True:
        with jobs_lock:
            job = None
            while job is None:
                running = sum(1 for j in jobs if j["status"] == "downloading")
                if running < max(1, int(config.get("concurrency") or 1)):
                    job = next((j for j in jobs if j["status"] == "queued"), None)
                if job is None:
                    jobs_lock.wait(timeout=5)
            job.update(status="downloading", error="", speed=0, cancel=False)
        try:
            download(job)
            job.update(status="done", speed=0, error="")
            local.record(job["rel"], job["provider"], job["item_id"], job["file_id"], job["item_name"],
                         job.get("size") or 0)
        except Cancelled:
            job.update(status="cancelled", speed=0)
            remove_partial(ROOT / job["rel"])
        except Exception as e:
            job.update(status="error", speed=0, error=str(e) or type(e).__name__)
        save_jobs()
        with jobs_lock:
            jobs_lock.notify_all()


def remove_quietly(path):
    try:
        path.unlink()
    except OSError:
        pass


def remove_partial(final):
    remove_quietly(final.with_name(final.name + ".part"))
    remove_quietly(final.with_name(final.name + ".part.aria2"))


def download(job):
    provider = get_provider(job["provider"])
    final = ROOT / job["rel"]
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            for _ in range(min(60, 5 * 2 ** (attempt - 1))):
                if job.get("cancel"):
                    raise Cancelled()
                time.sleep(1)
        if attempt == 3:
            # Repeated failures can come from a damaged partial file; start it again.
            remove_partial(final)
        try:
            # Links expire, so ask for a new one on every attempt.
            link = provider.link(job["item_id"], job["file_id"])
            url, real_name = link if isinstance(link, tuple) else (link, None)
            if job.get("rename") and real_name:
                final = final.with_name(clean_relpath(real_name)[-1])
                job.update(rel=rel_of(final), name=final.name, rename=False)
            if final.exists() and (not job["size"] or final.stat().st_size == job["size"]):
                job["done"] = final.stat().st_size
                return
            fetch(job, url, final)
            return
        except Cancelled:
            raise
        except ApiError as e:
            last_error = e
        except (urllib.error.URLError, OSError) as e:
            if isinstance(e, OSError) and e.errno in (28, 13, 30):  # full, denied, read-only
                raise ApiError(f"Cannot write to disk: {e.strerror}") from None
            last_error = e
        job["speed"] = 0
        job["error"] = f"Retrying ({attempt + 1}/{MAX_ATTEMPTS - 1}): {last_error}"
    raise ApiError(f"Gave up after {MAX_ATTEMPTS} attempts: {last_error}")


def fetch(job, url, final):
    part = final.with_name(final.name + ".part")
    # If the drive is unplugged, do not recreate the folder on the internal disk.
    if not ROOT.is_dir():
        raise OSError(30, f"Download folder {ROOT} is missing; is the drive connected?")
    final.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    control = part.with_name(part.name + ".aria2")
    if job["size"] and have > job["size"]:
        remove_partial(final)
        have = 0
    if job["size"] and have == job["size"] and not control.exists():
        os.replace(part, final)
        job["done"] = have
        return
    free = shutil.disk_usage(final.parent).free
    if job["size"] and not control.exists() and job["size"] - have > free:
        raise OSError(28, "Not enough free space on the drive")

    if aria2.start():
        fetch_aria2(job, url, part)
    else:
        if control.exists():
            # A file aria2 was writing has gaps, so it cannot be continued from its end.
            remove_partial(final)
            have = 0
        fetch_builtin(job, url, part, have)
    os.replace(part, final)
    remove_quietly(control)


def fetch_aria2(job, url, part):
    conns = max(1, min(16, int(config.get("connections") or 1)))
    gid = aria2.call("addUri", [url], {
        "dir": str(part.parent), "out": part.name, "split": str(conns),
        "max-connection-per-server": str(conns), "min-split-size": "5M",
    })
    keys = ["status", "completedLength", "totalLength", "downloadSpeed", "errorCode", "errorMessage"]
    finished = False
    try:
        while True:
            time.sleep(1)
            st = aria2.call("tellStatus", gid, keys)
            total = int(st.get("totalLength") or 0)
            if total:
                job["size"] = total
            job["done"] = int(st.get("completedLength") or 0)
            job["speed"] = int(st.get("downloadSpeed") or 0)
            if job["done"]:
                job["error"] = ""
            if job.get("cancel"):
                raise Cancelled()
            if st["status"] == "complete":
                finished = True
                return
            if st["status"] in ("error", "removed"):
                raise OSError(st.get("errorMessage") or f"aria2 error {st.get('errorCode')}")
    finally:
        try:
            if not finished:
                aria2.call("forceRemove", gid)
                for _ in range(25):
                    if aria2.call("tellStatus", gid, ["status"])["status"] != "active":
                        break
                    time.sleep(0.2)
            aria2.call("removeDownloadResult", gid)
        except OSError:
            pass


def fetch_builtin(job, url, part, have):
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0
        length = r.headers.get("Content-Length")
        total = have + int(length) if length and length.isdigit() else 0
        if total:
            job["size"] = total
        job["done"] = have
        window_start, window_bytes = time.monotonic(), 0
        with open(part, "ab" if have else "wb") as f:
            while True:
                if job.get("cancel"):
                    raise Cancelled()
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                job["done"] += len(chunk)
                window_bytes += len(chunk)
                elapsed = time.monotonic() - window_start
                if elapsed >= 1.5:
                    job["speed"] = int(window_bytes / elapsed)
                    window_start, window_bytes = time.monotonic(), 0
    if total and part.stat().st_size != total:
        raise OSError("Connection closed before the file was complete")


def job_action(action, job_id):
    with jobs_lock:
        if action == "clear":
            jobs[:] = [j for j in jobs if j["status"] in ("queued", "downloading")]
        elif action == "cancel_all":
            for j in jobs:
                if j["status"] == "queued":
                    j["status"] = "cancelled"
                elif j["status"] == "downloading":
                    j["cancel"] = True
        else:
            job = next((j for j in jobs if j["id"] == job_id), None)
            if job is None:
                raise ApiError("Download not found")
            if action == "cancel":
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                elif job["status"] == "downloading":
                    job["cancel"] = True
            elif action == "retry":
                if job["status"] in ("error", "cancelled"):
                    job.update(status="queued", error="")
            elif action == "remove":
                if job["status"] not in ("queued", "downloading"):
                    jobs.remove(job)
            else:
                raise ApiError("Unknown action")
        jobs_lock.notify_all()
    save_jobs()


# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DebridFetch"

    def log_message(self, fmt, *args):
        pass

    def send_body(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                self.send_body(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self.send_json(state())
            elif url.path == "/api/items":
                name = q.get("provider", "")
                items, _ = list_items(name, q.get("refresh") == "1")
                self.send_json({"items": local.annotate(name, items)})
            elif url.path == "/api/files":
                name, item_id = q.get("provider", ""), q.get("id", "")
                item = {"id": item_id, "name": q.get("name", ""), "files": get_provider(name).files(item_id)}
                self.send_json({"files": local.annotate(name, [item])[0]["files"]})
            elif url.path == "/api/folders":
                self.send_json(folders(q.get("path", "")))
            elif url.path == "/api/local":
                self.send_json(local_list(q.get("path", "")))
            elif url.path == "/api/orphans":
                self.send_json(orphans(q.get("refresh") == "1"))
            elif url.path == "/api/jobs":
                with jobs_lock:
                    self.send_json({"jobs": [public_job(j) for j in jobs]})
            else:
                self.send_json({"error": "Not found"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": f"Server error: {e}"}, 500)

    def do_POST(self):
        try:
            # Browsers cannot send this header and content type from another site
            # without a CORS preflight, which this server never grants.
            if (self.headers.get("X-Debrid-Fetch") != "1"
                    or "application/json" not in (self.headers.get("Content-Type") or "")):
                return self.send_json({"error": "Request refused"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/settings":
                self.send_json(update_settings(body))
            elif path == "/api/mkdir":
                parent = resolve_under_root(body.get("path", ""))
                name = clean_relpath(body.get("name", ""))
                if len(name) != 1:
                    raise ApiError("Enter a single folder name")
                (parent / name[0]).mkdir(parents=True, exist_ok=True)
                self.send_json({"path": rel_of(parent / name[0])})
            elif path == "/api/download":
                count = enqueue(body.get("provider", ""), body.get("dest", ""), body.get("items") or [])
                self.send_json({"queued": count})
            elif path == "/api/jobs/action":
                job_action(body.get("action"), body.get("id"))
                self.send_json({"ok": True})
            elif path == "/api/local/delete":
                count = local_delete(body.get("paths") or [], bool(body.get("prune")))
                self.send_json({"deleted": count})
            elif path == "/api/local/rename":
                local_rename(body.get("path", ""), body.get("name", ""))
                self.send_json({"ok": True})
            elif path == "/api/local/move":
                local_move(body.get("paths") or [], body.get("dest", ""))
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Not found"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)
        except (ValueError, OSError, shutil.Error) as e:
            self.send_json({"error": str(e)}, 400)


def state():
    try:
        usage = shutil.disk_usage(ROOT)
        free, total = usage.free, usage.total
    except OSError:
        free = total = 0
    running = aria2.proc is not None and aria2.proc.poll() is None
    return {
        "root": str(ROOT), "root_label": ROOT_LABEL, "root_ok": ROOT.is_dir() and os.access(ROOT, os.W_OK),
        "free": free, "total": total, "concurrency": config["concurrency"], "connections": config["connections"],
        "providers": {name: p.configured() for name, p in PROVIDERS.items()},
        "engine": f"aria2 {aria2.version}" if running else "built-in downloader",
        "engine_note": "" if running else aria2.error,
    }


def folders(rel):
    here = resolve_under_root(rel)
    if not here.is_dir():
        here = ROOT
    names = sorted((p.name for p in here.iterdir() if p.is_dir() and not p.name.startswith(".")),
                   key=str.lower)
    return {"path": "" if here == ROOT else rel_of(here), "folders": names}


def update_settings(body):
    result = {}
    with config_lock:
        for field, provider in (("torbox_key", "torbox"), ("rd_key", "realdebrid")):
            if field not in body:
                continue
            key = str(body[field]).strip()
            if key:
                result[provider] = PROVIDERS[provider].check(key)
            config[field] = key
            list_cache.pop(provider, None)
        if "concurrency" in body:
            config["concurrency"] = max(1, min(6, int(body["concurrency"])))
        if "connections" in body:
            config["connections"] = max(1, min(16, int(body["connections"])))
        save_config()
    with jobs_lock:
        jobs_lock.notify_all()
    return {"ok": True, "accounts": result, **state()}


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_config()
    local.load()
    load_jobs()
    threading.Thread(target=aria2.start, daemon=True).start()
    for i in range(6):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"Debrid Fetch on http://{HOST}:{PORT}, downloading to {ROOT}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    save_jobs()
    aria2.stop()


if __name__ == "__main__":
    main()
