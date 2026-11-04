#!/usr/bin/env python3
"""Addon veröffentlichen – kleines Tool mit Oberfläche im Browser.

Start:  Doppelklick auf „Veröffentlichen.command“ (Mac) oder  python3 tools/veroeffentlichen.py
Demo:   python3 tools/veroeffentlichen.py --demo   (alles echt gebaut, aber GitHub nur nachgespielt)

Du wählst nur das Addon und die Version, schreibst kurz, was neu ist, und tippst auf „Veröffentlichen“.
Das Tool erledigt den Rest:
  1. prüft Version, Schlüssel und GitHub
  2. trägt Version, Stufe und Änderungen in addon.json und CHANGELOG.md ein
  3. baut die ZIP-Datei (gleiche Dateien ergeben immer die gleiche Prüfsumme)
  4. baut den Katalog mit allen Addons und signiert ihn mit deinem Entwickler-Schlüssel
  5. lädt den Quelltext des Addons ins Repository
  6. legt das Release mit dem passenden Tag an und hängt die ZIP an
  7. lädt catalog.json und catalog.sig hoch
  8. lädt die ZIP von GitHub zurück und vergleicht die Prüfsumme

Beim ersten Start fragt es nach einem GitHub-Token und (wahlweise) nach deinem privaten Schlüssel. Beides liegt dann
im Schlüsselbund des Mac, nicht in einer Datei. Fehlt das Repository noch, legt das Tool es an und lädt alles hoch.
Die Oberfläche läuft nur auf deinem Rechner (127.0.0.1) und ist mit einem Zufallsschlüssel geschützt.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build as B  # noqa: E402

ROOT = B.ROOT
OWNER, NAME = B.REPO.split("/")
STAGES = [("dev", "In Entwicklung"), ("beta", "Beta"), ("vorab", "Vorabversion"), ("stabil", "Stabil")]
KC = "strom-waerme-addons"
SKIP = {"__pycache__", ".DS_Store"}
UPLOAD_TOP = ["README.md", ".gitignore", "Veröffentlichen.command", "tools/build.py", "tools/veroeffentlichen.py"]


# ------------------------------------------------------------------ Schlüsselbund
def _cfg_file(acct: str) -> pathlib.Path:
    return pathlib.Path.home() / ".config" / KC / acct


def kc_get(acct: str) -> str:
    if sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", KC, "-a", acct, "-w"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    f = _cfg_file(acct)
    return f.read_text().strip() if f.exists() else ""


def kc_set(acct: str, val: str) -> None:
    if sys.platform == "darwin":  # über stdin, damit das Geheimnis nicht in der Prozessliste steht
        cmd = f'add-generic-password -U -s {KC} -a {acct} -w "{val}"\n'
        subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, check=False)
        return
    f = _cfg_file(acct)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(val)
    os.chmod(f, 0o600)


def kc_del(acct: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["security", "delete-generic-password", "-s", KC, "-a", acct], capture_output=True)
    else:
        _cfg_file(acct).unlink(missing_ok=True)


# ------------------------------------------------------------------ Versionen
def vkey(v: str) -> tuple[int, ...]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def suggest(v: str) -> dict[str, str]:
    a, b, c = vkey(v)
    return {"fix": f"{a}.{b}.{c + 1}", "neu": f"{a}.{b + 1}.0", "gross": f"{a + 1}.0.0"}


def key_ok(priv: str) -> bool:
    from cryptography.hazmat.primitives import serialization as S
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        k = Ed25519PrivateKey.from_private_bytes(B.b64d(priv.strip()[len("FHPRIV1-"):]))
        return B.b64e(k.public_key().public_bytes(S.Encoding.Raw, S.PublicFormat.Raw)) == os.environ.get("FH_PUB", B.PUBLIC_KEY)
    except Exception:  # noqa: BLE001
        return False


def sign(priv: str, raw: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return B.b64e(Ed25519PrivateKey.from_private_bytes(B.b64d(priv.strip()[len("FHPRIV1-"):])).sign(raw))


# ------------------------------------------------------------------ GitHub
class GHError(Exception):
    pass


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token

    def _req(self, method: str, url: str, data=None, raw: bytes | None = None, ctype: str = "application/json", ok=(200, 201)):
        if not url.startswith("http"):
            url = os.environ.get("SW_API", "https://api.github.com") + url
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        rq = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "strom-waerme-addons-tool", **({"Content-Type": ctype} if body is not None else {})})
        try:
            with urllib.request.urlopen(rq, timeout=90) as r:
                txt = r.read()
                return r.status, (json.loads(txt) if txt and r.headers.get("Content-Type", "").startswith("application/json") else txt)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(txt).get("message", txt)
            except ValueError:
                msg = txt
            if e.code in ok:
                return e.code, msg
            raise GHError(f"GitHub {e.code}: {msg}"[:300]) from e
        except urllib.error.URLError as e:
            raise GHError(f"GitHub nicht erreichbar: {e.reason}") from e

    def login(self) -> str:
        return self._req("GET", "/user")[1]["login"]

    def repo_exists(self) -> bool:
        try:
            self._req("GET", f"/repos/{OWNER}/{NAME}")
            return True
        except GHError as e:
            if "404" in str(e):
                return False
            raise

    def create_repo(self) -> None:
        self._req("POST", "/user/repos", {"name": NAME, "description": "Addon-Katalog für Strom & Wärme", "private": False, "auto_init": True, "has_wiki": False})
        for _ in range(10):  # GitHub braucht einen Moment, bis main da ist
            try:
                self._req("GET", f"/repos/{OWNER}/{NAME}/git/ref/heads/main")
                return
            except GHError:
                time.sleep(1.5)

    def get_file(self, path: str) -> bytes | None:
        try:
            _, d = self._req("GET", f"/repos/{OWNER}/{NAME}/contents/{path}?ref=main")
        except GHError as e:
            if "404" in str(e):
                return None
            raise
        return base64.b64decode(d["content"]) if isinstance(d, dict) and d.get("content") is not None else None

    def commit(self, files: dict[str, bytes], msg: str) -> str:
        _, ref = self._req("GET", f"/repos/{OWNER}/{NAME}/git/ref/heads/main")
        head = ref["object"]["sha"]
        _, c = self._req("GET", f"/repos/{OWNER}/{NAME}/git/commits/{head}")
        tree = []
        for p, data in sorted(files.items()):
            _, b = self._req("POST", f"/repos/{OWNER}/{NAME}/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            tree.append({"path": p, "mode": "100755" if p.endswith(".command") else "100644", "type": "blob", "sha": b["sha"]})
        _, t = self._req("POST", f"/repos/{OWNER}/{NAME}/git/trees", {"base_tree": c["tree"]["sha"], "tree": tree})
        _, nc = self._req("POST", f"/repos/{OWNER}/{NAME}/git/commits", {"message": msg, "tree": t["sha"], "parents": [head]})
        self._req("PATCH", f"/repos/{OWNER}/{NAME}/git/refs/heads/main", {"sha": nc["sha"]})
        return nc["sha"]

    def tag_exists(self, tag: str) -> bool:
        try:
            self._req("GET", f"/repos/{OWNER}/{NAME}/releases/tags/{tag}")
            return True
        except GHError as e:
            if "404" in str(e):
                return False
            raise

    def release(self, tag: str, sha: str, title: str, body: str, pre: bool) -> dict:
        return self._req("POST", f"/repos/{OWNER}/{NAME}/releases", {"tag_name": tag, "target_commitish": sha, "name": title, "body": body, "prerelease": pre})[1]

    def upload(self, rel: dict, name: str, data: bytes) -> str:
        url = rel["upload_url"].split("{")[0] + f"?name={name}"
        _, a = self._req("POST", url, raw=data, ctype="application/zip")
        return a["browser_download_url"]

    def download(self, url: str) -> bytes:
        rq = urllib.request.Request(url, headers={"User-Agent": "strom-waerme-addons-tool"})
        with urllib.request.urlopen(rq, timeout=90) as r:
            return r.read()


class FakeGitHub:
    """Für --demo: spielt GitHub nach, schreibt nichts nach außen."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.tags: dict[str, bytes] = {}
        self.exists = True
        self.n = 0
        cat = ROOT / "catalog.json"
        if cat.exists():
            self.files["catalog.json"] = cat.read_bytes()

    def login(self) -> str:
        return OWNER

    def repo_exists(self) -> bool:
        return self.exists

    def create_repo(self) -> None:
        time.sleep(0.6)
        self.exists = True

    def get_file(self, p: str) -> bytes | None:
        return self.files.get(p)

    def commit(self, files: dict[str, bytes], msg: str) -> str:
        time.sleep(0.8)
        self.files.update(files)
        self.n += 1
        return hashlib.sha1(f"{self.n}{msg}".encode()).hexdigest()

    def tag_exists(self, tag: str) -> bool:
        return tag in self.tags

    def release(self, tag, sha, title, body, pre) -> dict:
        time.sleep(0.6)
        return {"tag": tag, "html_url": f"https://github.com/{OWNER}/{NAME}/releases/tag/{tag}"}

    def upload(self, rel: dict, name: str, data: bytes) -> str:
        time.sleep(0.8)
        self.tags[rel["tag"]] = data
        return f"https://github.com/{OWNER}/{NAME}/releases/download/{rel['tag']}/{name}"

    def download(self, url: str) -> bytes:
        time.sleep(0.4)
        return self.tags[url.split("/download/")[1].split("/")[0]]


# ------------------------------------------------------------------ Addons lesen
def addon_files(aid: str) -> dict[str, bytes]:
    d = ROOT / "addons" / aid
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*"))
            if p.is_file() and not (set(p.parts) & SKIP) and not p.name.endswith(".pyc")}


def addons() -> list[dict]:
    out = []
    for d in sorted((ROOT / "addons").iterdir()):
        f = d / "addon.json"
        if f.exists():
            m = json.loads(f.read_text(encoding="utf-8"))
            out.append({k: m.get(k) for k in ("id", "n", "name", "version", "build", "stage", "tag")})
    return out


def zip_bytes(aid: str, files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, data in sorted(files.items()):
            zi = zipfile.ZipInfo(f"{aid}/{rel}", B.FIXED)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, data)
    return b.getvalue()


def changelog_add(old: str, version: str, build: int, notes: list[str], name: str) -> str:
    """Neuen Abschnitt oben einfügen. Gibt es diese Version mit diesem Build schon, wird ihr Abschnitt ersetzt,
    doppelte Abschnitte derselben Version fallen weg."""
    head = f"## [{version}] Build {build} - {datetime.now().strftime('%Y-%m-%d')}\n" + "".join(f"- {n}\n" for n in notes) + "\n"
    if not old.strip():
        return f"# Modul {name}\n\n" + head
    parts = re.split(r"(?m)^(?=## \[)", old)
    title, secs = parts[0], parts[1:]
    seen, keep = set(), []
    for sec in secs:
        m = re.match(r"## \[([^\]]+)\](?: Build (\d+))?", sec)
        key = (m.group(1), int(m.group(2) or 1)) if m else None
        if key == (version, build) or (key and key in seen):
            continue
        if key:
            seen.add(key)
        keep.append(sec if sec.endswith("\n\n") else sec.rstrip("\n") + "\n\n")
    return (title if title.endswith("\n\n") else title.rstrip("\n") + "\n\n") + head + "".join(keep).rstrip("\n") + "\n"


# ------------------------------------------------------------------ Ablauf
STEPS = [
    ("check", "Prüfen", "Version, Schlüssel, GitHub"),
    ("meta", "Version und Änderungen eintragen", "addon.json und CHANGELOG.md"),
    ("zip", "ZIP bauen", ""),
    ("cat", "Katalog bauen und signieren", ""),
    ("src", "Quelltext hochladen", "Commit ins Repository"),
    ("rel", "Release anlegen und ZIP anhängen", ""),
    ("pub", "Katalog veröffentlichen", "catalog.json und catalog.sig"),
    ("verify", "Kontrolle", "ZIP von GitHub laden und Prüfsumme vergleichen"),
]


class Job:
    def __init__(self) -> None:
        self.steps = [{"k": k, "t": t, "s": s, "st": "wait", "info": ""} for k, t, s in STEPS]
        self.done = False
        self.ok = False
        self.err = ""
        self.result: dict = {}
        self.t_end = 0.0

    def step(self, k: str, st: str, info: str = "") -> None:
        for s in self.steps:
            if s["k"] == k:
                s["st"], s["info"] = st, info or s["info"]

    def snap(self) -> dict:
        return {"steps": self.steps, "done": self.done, "ok": self.ok, "err": self.err, "result": self.result}


def run_publish(job: Job, gh, p: dict, priv: str) -> None:
    cur = "check"
    try:
        aid, ver, dry = p["id"], p["version"].strip(), bool(p.get("dry"))
        build = max(1, int(p.get("build") or 1))
        stage = p.get("stage") if p.get("stage") in dict(STAGES) else "dev"
        notes = [re.sub(r"^\s*[-•*]\s*", "", n).strip() for n in str(p.get("notes") or "").splitlines() if n.strip()][:8]
        job.step("check", "run")
        files = addon_files(aid)
        meta = json.loads(files["addon.json"])
        if not re.match(r"^\d+\.\d+\.\d+$", ver):
            raise ValueError("Version bitte als Zahl.Zahl.Zahl, zum Beispiel 0.1.2")
        cat_old = gh.get_file("catalog.json") if (gh and not dry) else None
        rem = next((x for x in (json.loads(cat_old)["addons"] if cat_old else []) if x.get("id") == aid), None)
        if rem and (vkey(ver), build) <= (vkey(rem["version"]), int(rem.get("build") or 1)):
            raise ValueError(f"Im Katalog steht schon {rem['version']} Build {rem.get('build', 1)}. Die neue Version muss größer sein.")
        if (vkey(ver), build) < (vkey(meta["version"]), int(meta.get("build") or 1)) or (rem and (vkey(ver), build) == (vkey(meta["version"]), int(meta.get("build") or 1))):
            raise ValueError(f"Die neue Version muss größer sein als {meta['version']} Build {meta.get('build', 1)}")
        if not notes:
            raise ValueError("Bitte mindestens einen Punkt unter „Was ist neu“ eintragen")
        if not key_ok(priv):
            raise ValueError("Der private Schlüssel passt nicht zum öffentlichen Schlüssel der Integration")
        tag = f"{aid}-{ver}-b{build}"
        if not dry and not gh.repo_exists():
            raise ValueError(f"Das Repository {OWNER}/{NAME} gibt es noch nicht. Oben auf „Repository anlegen“ tippen.")
        if not dry and gh.tag_exists(tag):
            raise ValueError(f"Das Release {tag} gibt es schon. Bitte eine höhere Version oder einen neuen Build wählen.")
        job.step("check", "ok", f"{meta['version']} Build {meta.get('build', 1)} → {ver} Build {build} · Tag {tag}")

        cur = "meta"
        job.step("meta", "run")
        meta.update(version=ver, build=build, stage=stage)
        files["addon.json"] = (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode()
        files["CHANGELOG.md"] = changelog_add(files.get("CHANGELOG.md", b"").decode(), ver, build, notes, f"{meta.get('n')} {meta.get('name')}").encode()
        job.step("meta", "ok", f"{len(notes)} {'Punkt' if len(notes) == 1 else 'Punkte'} · Stufe {dict(STAGES)[stage]}")

        cur = "zip"
        job.step("zip", "run")
        data = zip_bytes(aid, files)
        sha = hashlib.sha256(data).hexdigest()
        (ROOT / "dist").mkdir(exist_ok=True)
        (ROOT / "dist" / f"{tag}.zip").write_bytes(data)
        job.step("zip", "ok", f"{tag}.zip · {len(data) / 1024:.1f} kB · SHA-256 {sha[:12]}…")

        cur = "cat"
        job.step("cat", "run")
        old = cat_old or ((ROOT / "catalog.json").read_bytes() if (ROOT / "catalog.json").exists() else None)
        entries = json.loads(old)["addons"] if old else []
        e = {k: meta[k] for k in B.FIELDS if k in meta}
        e.update(url=f"https://github.com/{B.REPO}/releases/download/{tag}/{tag}.zip", sha256=sha, size=len(data), notes=notes, date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        entries = [x for x in entries if x.get("id") != aid] + [e]
        entries.sort(key=lambda x: (x.get("n", 99), x.get("id")))
        cat = {"v": 1, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": B.REPO, "addons": entries}
        raw = (json.dumps(cat, ensure_ascii=False, indent=2) + "\n").encode()
        sig = (sign(priv, raw) + "\n").encode()
        job.step("cat", "ok", f"{len(entries)} {'Addon' if len(entries) == 1 else 'Addons'} · Unterschrift ✓")

        if dry:
            for k in ("src", "rel", "pub", "verify"):
                job.step(k, "skip", "Probelauf: nichts hochgeladen")
            (ROOT / "dist" / "catalog.probelauf.json").write_bytes(raw)
            job.result = {"dry": True, "tag": tag, "zip": f"dist/{tag}.zip"}
            job.ok = True
            return

        cur = "src"
        job.step("src", "run")
        csha = gh.commit({f"addons/{aid}/{r}": d for r, d in files.items()}, f"{meta['name']} {ver} Build {build}")
        job.step("src", "ok", f"Commit {csha[:7]}")

        cur = "rel"
        job.step("rel", "run")
        rel = gh.release(tag, csha, f"Modul {meta.get('n')} {meta['name']} {ver} Build {build}", "\n".join(f"- {n}" for n in notes), stage != "stabil")
        url = gh.upload(rel, f"{tag}.zip", data)
        job.step("rel", "ok", f"Release {tag} · ZIP angehängt")

        cur = "pub"
        job.step("pub", "run")
        gh.commit({"catalog.json": raw, "catalog.sig": sig}, f"Katalog: {meta['name']} {ver} Build {build}")
        job.step("pub", "ok", "catalog.json und catalog.sig auf main")

        cur = "verify"
        job.step("verify", "run")
        back = gh.download(url)
        if hashlib.sha256(back).hexdigest() != sha:
            raise ValueError("Die ZIP auf GitHub hat eine andere Prüfsumme. Bitte das Release prüfen.")
        job.step("verify", "ok", "Prüfsumme stimmt")

        # erst jetzt lokal übernehmen
        d = ROOT / "addons" / aid
        (d / "addon.json").write_bytes(files["addon.json"])
        (d / "CHANGELOG.md").write_bytes(files["CHANGELOG.md"])
        (ROOT / "catalog.json").write_bytes(raw)
        (ROOT / "catalog.sig").write_bytes(sig)
        job.result = {"dry": False, "tag": tag, "release": rel.get("html_url", ""), "catalog": f"https://github.com/{B.REPO}/blob/main/catalog.json"}
        job.ok = True
    except Exception as ex:  # noqa: BLE001
        job.step(cur, "err", str(ex))
        job.err = str(ex)
        if not isinstance(ex, (ValueError, GHError)):
            traceback.print_exc()
    finally:
        job.done = True
        job.t_end = time.time()


def run_setup_repo(job: Job, gh) -> None:
    try:
        job.steps = [{"k": "repo", "t": "Repository anlegen", "s": f"{OWNER}/{NAME}, öffentlich", "st": "run", "info": ""},
                     {"k": "files", "t": "Alle Dateien hochladen", "s": "README, Tools, Addons", "st": "wait", "info": ""}]
        if not gh.repo_exists():
            gh.create_repo()
        job.step("repo", "ok", "angelegt")
        job.step("files", "run")
        files = {p: (ROOT / p).read_bytes() for p in UPLOAD_TOP if (ROOT / p).exists()}
        for a in addons():
            files.update({f"addons/{a['id']}/{r}": d for r, d in addon_files(a["id"]).items()})
        gh.commit(files, "Addon-Katalog: Grundgerüst")
        job.step("files", "ok", f"{len(files)} Dateien")
        job.ok = True
    except Exception as ex:  # noqa: BLE001
        for s in job.steps:
            if s["st"] == "run":
                s["st"], s["info"] = "err", str(ex)
        job.err = str(ex)
    finally:
        job.done = True
        job.t_end = time.time()


# ------------------------------------------------------------------ Server
class App:
    def __init__(self, demo: bool) -> None:
        self.demo = demo
        self.secret = secrets.token_urlsafe(18)
        self.job: Job | None = None
        self.mem_key = ""
        self._gh = FakeGitHub() if demo else None
        self._cache: tuple[float, dict] | None = None
        if demo:
            self.mem_key = os.environ.get("FH_PRIV", "")

    def gh(self):
        if self.demo:
            return self._gh
        t = kc_get("github")
        return GitHub(t) if t else None

    def key(self) -> str:
        return self.mem_key or kc_get("schluessel")

    def state(self) -> dict:
        busy = self.job is not None and not self.job.done
        fresh_job = self.job is not None and self.job.done and self.job.t_end > (self._cache[0] if self._cache else 0)
        if self._cache and not fresh_job and (busy or time.time() - self._cache[0] < 60):  # GitHub nicht bei jedem Blick fragen
            return {**self._cache[1], "addons": [{**a, "remote": self._cache[1]["_remote"].get(a["id"], ""), "sug": suggest(a["version"])} for a in addons()],
                    "key": bool(self.key()), "job": self.job.snap() if self.job else None}
        gh, login, repo, err = self.gh(), "", None, ""
        if gh:
            try:
                login = gh.login()
                repo = gh.repo_exists()
            except GHError as e:
                err = str(e)
        try:
            import cryptography  # noqa: F401
            crypto = True
        except ImportError:
            crypto = False
        remote = {}
        if gh and repo:
            try:
                raw = gh.get_file("catalog.json")
                remote = {x["id"]: f"{x['version']} Build {x.get('build', 1)}" for x in json.loads(raw)["addons"]} if raw else {}
            except Exception:  # noqa: BLE001
                remote = {}
        d = {"demo": self.demo, "repo_name": B.REPO, "token": bool(gh), "login": login, "repo": repo, "gh_err": err,
             "key_saved": bool(kc_get("schluessel")) if not self.demo else True, "crypto": crypto, "stages": STAGES, "_remote": remote}
        self._cache = (time.time(), d)
        return {**d, "addons": [{**a, "remote": remote.get(a["id"], ""), "sug": suggest(a["version"])} for a in addons()],
                "key": bool(self.key()), "job": self.job.snap() if self.job else None}


HTML = r"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Addon veröffentlichen</title>
<style>
:root{--bg:#0e141b;--card:#151e28;--line:#273444;--mut:#8ea2b5;--txt:#e7eef5;--teal:#2dd4bf;--amb:#f59e0b;--red:#f87171;--ok:#34d399}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 -apple-system,Inter,system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 60px}h1{font-size:1.5rem;margin:0}.top{display:flex;align-items:center;gap:14px;margin-bottom:22px}
.lg{width:46px;height:46px;border-radius:14px;background:linear-gradient(135deg,#14b8a6,#0c4a6e);display:grid;place-items:center;font-size:24px}
.top small{color:var(--mut)}.demo{margin-left:auto;background:#7c3aed;color:#fff;font-size:.75rem;font-weight:700;padding:4px 10px;border-radius:999px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px 20px;margin-bottom:14px}.card h2{font-size:1rem;margin:0 0 12px;display:flex;align-items:center;gap:8px}
.card h2 em{margin-left:auto;font-style:normal;font-size:.78rem;color:var(--mut);font-weight:500}
.chk{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.ci{border:1px solid var(--line);border-radius:14px;padding:10px 12px}.ci b{display:block;font-size:.9rem}.ci small{color:var(--mut);font-size:.78rem}
.ci.ok b::before{content:"✓ ";color:var(--ok)}.ci.wa{border-color:var(--amb)}.ci.wa b::before{content:"! ";color:var(--amb)}
.ads{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}.ad{border:1px solid var(--line);border-radius:16px;padding:12px 14px;background:transparent;color:inherit;text-align:left;font:inherit;cursor:pointer}
.ad.sel{border-color:var(--teal);box-shadow:0 0 0 2px color-mix(in srgb,var(--teal) 35%,transparent)}.ad b{display:block}.ad small{color:var(--mut);font-size:.8rem;display:block}
.vs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}.vb{border:1px solid var(--line);border-radius:12px;padding:8px 12px;background:transparent;color:inherit;font:inherit;cursor:pointer;text-align:left}.vb b{display:block}.vb small{color:var(--mut);font-size:.75rem}
.vb.sel{border-color:var(--teal);background:color-mix(in srgb,var(--teal) 14%,transparent)}
.row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}label{font-size:.8rem;color:var(--mut);display:flex;flex-direction:column;gap:5px}
input,textarea{font:inherit;color:var(--txt);background:#0b1118;border:1px solid var(--line);border-radius:10px;padding:9px 11px}input.v{width:130px}input.bd{width:70px}textarea{width:100%;min-height:96px;resize:vertical}
.seg{display:flex;border:1px solid var(--line);border-radius:12px;overflow:hidden}.seg button{background:transparent;border:0;color:var(--mut);padding:9px 12px;font:inherit;font-size:.85rem;font-weight:600;cursor:pointer}.seg button.sel{background:var(--teal);color:#042f2e}
.go{display:flex;align-items:center;gap:14px;margin-top:6px;flex-wrap:wrap}.btn{border:0;border-radius:14px;padding:13px 22px;font:inherit;font-weight:700;cursor:pointer;background:var(--teal);color:#042f2e}.btn:disabled{opacity:.5;cursor:default}
.btn.sec{background:transparent;border:1px solid var(--line);color:var(--txt)}label.sw{flex-direction:row;display:flex;align-items:center;gap:8px;color:var(--mut);font-size:.9rem;cursor:pointer}.sw input{width:18px;height:18px}
.st{display:flex;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}.st:last-child{border:0}.st i{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-style:normal;font-size:.8rem;font-weight:800;border:2px solid var(--line);color:var(--mut)}
.st.ok i{background:var(--ok);border-color:var(--ok);color:#052e1f}.st.run i{border-color:var(--teal);border-top-color:transparent;animation:sp 1s linear infinite}.st.err i{background:var(--red);border-color:var(--red);color:#fff}.st.skip i{opacity:.5}
@keyframes sp{to{transform:rotate(360deg)}}.st b{display:block;font-size:.92rem}.st small{color:var(--mut);font-size:.8rem}.st.err small{color:var(--red)}
.done{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 8%,var(--card))}.fail{border-color:var(--red);background:color-mix(in srgb,var(--red) 8%,var(--card))}.done a{color:var(--teal)}
.hint{font-size:.82rem;color:var(--mut);margin-top:8px}.err{color:var(--red);font-size:.88rem;margin-top:8px}.set input{width:100%}
code{background:#0b1118;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:.85em}
</style></head><body><div class="wrap" id="app">Lädt …</div>
<script>
const S = location.search.slice(1); let st = null, sel = null, ver = "", bld = 1, stage = "dev", notes = "", dry = false, setupOpen = false, msg = "";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = (p, d) => fetch("/api/" + p, { method: d ? "POST" : "GET", headers: { "X-Key": S, "Content-Type": "application/json" }, body: d ? JSON.stringify(d) : undefined }).then((r) => r.json());
let wasBusy = false;
const load = () => api("state").then((r) => { st = r; if (!sel && r.addons.length) pick(r.addons[0].id, true);
  const busy = r.job && !r.job.done; if (wasBusy && !busy && r.job.ok && !r.job.result.dry) { pick(sel, true); notes = ""; } wasBusy = !!busy;
  render(); if (busy) setTimeout(load, 700); });
function pick(id, quiet) { sel = id; const a = st.addons.find((x) => x.id === id); if (a.remote) { ver = a.sug.fix; bld = 1; } else { ver = a.version; bld = a.build; } stage = a.stage || "dev"; if (!quiet) render(); }
function render() {
  const A = st.addons.find((x) => x.id === sel), J = st.job, busy = J && !J.done;
  const ready = st.token && st.login && st.repo && st.key && st.crypto;
  const ci = (ok, t, s) => `<div class="ci ${ok ? "ok" : "wa"}"><b>${t}</b><small>${s}</small></div>`;
  let h = `<div class="top"><div class="lg">🧩</div><div><h1>Addon veröffentlichen</h1><small>Strom &amp; Wärme · Katalog ${esc(st.repo_name)}</small></div>${st.demo ? '<span class="demo">DEMO · GitHub nur nachgespielt</span>' : ""}</div>`;
  h += `<div class="card"><h2>Verbindung<em><button class="btn sec" style="padding:5px 12px;font-size:.8rem" onclick="setupOpen=!setupOpen;render()">${setupOpen ? "Schließen" : "Einrichten"}</button></em></h2><div class="chk">
    ${ci(st.token && st.login, "GitHub", st.login ? "angemeldet als " + esc(st.login) : st.gh_err ? esc(st.gh_err) : "Token fehlt")}
    ${ci(st.repo, "Repository", st.repo ? esc(st.repo_name) : st.repo === false ? "gibt es noch nicht" : "unbekannt")}
    ${ci(st.key && st.crypto, "Schlüssel", !st.crypto ? "cryptography fehlt (siehe Anleitung)" : st.key ? (st.key_saved ? "im Schlüsselbund" : "nur für diese Sitzung") : "fehlt")}</div>
    ${st.repo === false && st.login ? `<div class="go" style="margin-top:12px"><button class="btn" onclick="mkrepo()" ${busy ? "disabled" : ""}>Repository anlegen und alles hochladen</button><span class="hint">legt ${esc(st.repo_name)} öffentlich an</span></div>` : ""}
    ${setupOpen || !st.token || !st.key ? `<div class="set" style="margin-top:14px;display:grid;gap:12px">
      <label>GitHub-Token (Classic, Recht „public_repo“)<input type="password" id="tok" placeholder="ghp_…"></label>
      <label>Privater Schlüssel (FHPRIV1-…)<input type="password" id="pk" placeholder="FHPRIV1-…"></label>
      <label class="sw"><input type="checkbox" id="save" checked> Schlüssel im Schlüsselbund merken (sonst nur bis zum Schließen)</label>
      <div class="go"><button class="btn" onclick="setup()">Speichern und prüfen</button>${st.token || st.key_saved ? `<button class="btn sec" onclick="forget()">Alles vergessen</button>` : ""}</div>${msg ? `<div class="err">${esc(msg)}</div>` : ""}</div>` : ""}</div>`;
  h += `<div class="card"><h2>1 · Addon wählen</h2><div class="ads">${st.addons.map((a) => `<button class="ad ${a.id === sel ? "sel" : ""}" onclick="pick('${a.id}')"><b>Modul ${a.n} · ${esc(a.name)}</b><small>hier: v${esc(a.version)} Build ${a.build}</small><small>im Katalog: ${a.remote ? "v" + esc(a.remote) : "noch nicht"}</small></button>`).join("")}</div></div>`;
  if (A) {
    const S2 = A.sug, vb = (v, t, s) => `<button class="vb ${ver === v && bld === 1 ? "sel" : ""}" onclick="ver='${v}';bld=1;render()"><b>v${v}</b><small>${t}</small></button>`;
    h += `<div class="card"><h2>2 · Neue Version<em>bisher v${esc(A.version)} Build ${A.build}</em></h2><div class="vs">${!A.remote ? `<button class="vb ${ver === A.version && bld === A.build ? "sel" : ""}" onclick="ver='${A.version}';bld=${A.build};render()"><b>v${esc(A.version)}</b><small>erste Veröffentlichung</small></button>` : ""}${vb(S2.fix, "Fehlerbehebung")}${vb(S2.neu, "Neue Funktionen")}${vb(S2.gross, "Großer Schritt")}<button class="vb ${ver === A.version && bld === A.build + 1 ? "sel" : ""}" onclick="ver='${A.version}';bld=${A.build + 1};render()"><b>Build ${A.build + 1}</b><small>gleiche Version, nur neu gebaut</small></button></div>
      <div class="row"><label>Version<input class="v" value="${esc(ver)}" oninput="ver=this.value"></label><label>Build<input class="bd" type="number" min="1" value="${bld}" oninput="bld=+this.value"></label>
      <label>Stufe<div class="seg">${st.stages.map(([k, l]) => `<button class="${stage === k ? "sel" : ""}" onclick="stage='${k}';render()">${l}</button>`).join("")}</div></label></div></div>
      <div class="card"><h2>3 · Was ist neu<em>ein Punkt pro Zeile · erscheint in Home Assistant</em></h2><textarea id="nt" oninput="notes=this.value" placeholder="Raum-Kacheln sortieren sich nach Fälligkeit&#10;Behoben: …">${esc(notes)}</textarea></div>`;
    h += `<div class="card"><div class="go"><button class="btn" onclick="pub()" ${busy || (!ready && !dry) || !st.key ? "disabled" : ""}>${dry ? "Probelauf starten" : `v${esc(ver)} Build ${bld} veröffentlichen`}</button><label class="sw"><input type="checkbox" ${dry ? "checked" : ""} onchange="dry=this.checked;render()"> Probelauf (nur bauen, nichts hochladen)</label></div>
      <div class="hint">Tag: <code>${esc(sel)}-${esc(ver)}-b${bld}</code> · danach in Home Assistant unter Einstellungen › Addons auf „Jetzt prüfen“ tippen.</div></div>`;
  }
  if (J) {
    const fin = J.done ? (J.ok ? "done" : "fail") : "";
    h += `<div class="card ${fin}"><h2>${J.done ? (J.ok ? (J.result.dry ? "Probelauf fertig" : "Veröffentlicht") : "Abgebrochen") : "Läuft …"}</h2>${J.steps.map((s) => `<div class="st ${s.st}"><i>${s.st === "ok" ? "✓" : s.st === "err" ? "!" : s.st === "skip" ? "–" : ""}</i><div><b>${esc(s.t)}</b><small>${esc(s.info || s.s)}</small></div></div>`).join("")}
      ${J.done && J.ok && J.result.release ? `<div class="hint" style="margin-top:12px">Release: <a href="${esc(J.result.release)}" target="_blank">${esc(J.result.tag)}</a> · <a href="${esc(J.result.catalog)}" target="_blank">Katalog ansehen</a><br>In Home Assistant: Einstellungen › Addons › <b>Jetzt prüfen</b>, dann im Reiter Updates aktualisieren.</div>` : ""}
      ${J.done && J.ok && J.result.dry ? `<div class="hint" style="margin-top:12px">Die ZIP liegt in <code>${esc(J.result.zip)}</code>, der Katalog in <code>dist/catalog.probelauf.json</code>. Für echt: Haken bei Probelauf entfernen.</div>` : ""}
      ${J.done && !J.ok ? `<div class="hint" style="margin-top:12px">Es wurde nichts Halbes veröffentlicht: Der Katalog kommt immer als letzter Schritt.</div>` : ""}</div>`;
  }
  document.getElementById("app").innerHTML = h;
}
function pub() { api("publish", { id: sel, version: ver, build: bld, stage, notes, dry }).then((r) => { if (r.error) { alert(r.error); return; } load(); }); }
function mkrepo() { api("mkrepo", {}).then(load); }
function setup() { const t = document.getElementById("tok").value.trim(), k = document.getElementById("pk").value.trim(), s = document.getElementById("save").checked; api("setup", { token: t, key: k, save: s }).then((r) => { msg = r.error || ""; if (!msg) setupOpen = false; load(); }); }
function forget() { if (confirm("Token und Schlüssel aus dem Schlüsselbund löschen?")) api("forget", {}).then(load); }
load();
</script></body></html>"""


def make_handler(app: App):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # leise
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, d: dict, code: int = 200) -> None:
            self._send(code, json.dumps(d, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _auth(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Key", ""), app.secret)

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] == "/":
                if self.path.split("?", 1)[-1] != app.secret:
                    return self._send(403, "Bitte das Tool neu starten.".encode(), "text/plain; charset=utf-8")
                return self._send(200, HTML.encode(), "text/html; charset=utf-8")
            if self.path == "/api/state" and self._auth():
                return self._json(app.state())
            self._send(404, b"", "text/plain")

        def do_POST(self):  # noqa: N802
            if not self._auth() or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
                return self._send(403, b"", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            busy = app.job is not None and not app.job.done
            app._cache = None  # nach jeder Aktion frisch von GitHub lesen
            if self.path == "/api/publish":
                if busy:
                    return self._json({"error": "Es läuft schon etwas"})
                gh = app.gh()
                if gh is None and not d.get("dry"):
                    return self._json({"error": "Zuerst GitHub einrichten"})
                app.job = Job()
                threading.Thread(target=run_publish, args=(app.job, gh, d, app.key()), daemon=True).start()
                return self._json({"ok": True})
            if self.path == "/api/mkrepo":
                if busy or app.gh() is None:
                    return self._json({"error": "Nicht möglich"})
                app.job = Job()
                threading.Thread(target=run_setup_repo, args=(app.job, app.gh()), daemon=True).start()
                return self._json({"ok": True})
            if self.path == "/api/setup":
                t, k = str(d.get("token") or "").strip(), str(d.get("key") or "").strip()
                if t:
                    if app.demo:
                        pass
                    else:
                        try:
                            login = GitHub(t).login()
                        except GHError as e:
                            return self._json({"error": f"Token ungültig: {e}"})
                        if login.lower() != OWNER.lower():
                            return self._json({"error": f"Das Token gehört zu {login}, nicht zu {OWNER}"})
                        kc_set("github", t)
                if k:
                    if not key_ok(k):
                        return self._json({"error": "Der Schlüssel passt nicht zum öffentlichen Schlüssel der Integration"})
                    app.mem_key = k
                    if d.get("save") and not app.demo:
                        kc_set("schluessel", k)
                return self._json({"ok": True})
            if self.path == "/api/forget":
                kc_del("github")
                kc_del("schluessel")
                app.mem_key = ""
                return self._json({"ok": True})
            self._send(404, b"", "text/plain")

    return H


def main() -> None:
    demo = "--demo" in sys.argv
    app = App(demo)
    port = int(os.environ.get("SW_PORT") or 0)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{srv.server_port}/?{app.secret}"
    print(f"Addon veröffentlichen läuft: {url}\nZum Beenden dieses Fenster schließen oder Ctrl + C.")
    if os.environ.get("SW_URLFILE"):
        pathlib.Path(os.environ["SW_URLFILE"]).write_text(url)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
