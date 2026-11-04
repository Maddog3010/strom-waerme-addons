#!/usr/bin/env python3
"""Baut die Addons, den Katalog und seine Unterschrift.

  python3 tools/build.py            ZIP je Addon nach dist/, catalog.json neu, catalog.sig mit dem Entwickler-Schlüssel
  python3 tools/build.py --check    prüft catalog.json gegen catalog.sig und die ZIPs in dist/

Der private Schlüssel (FHPRIV1-…) ist derselbe wie für die Sponsor-Schlüssel. Er wird nie gespeichert:
  FH_PRIV=FHPRIV1-xxxx python3 tools/build.py
Ohne FH_PRIV fragt das Skript danach (Eingabe unsichtbar).

Danach je Addon auf GitHub ein Release mit dem Tag aus der Ausgabe anlegen (z. B. saugroboter-0.1.1-b1),
die ZIP-Datei aus dist/ anhängen und catalog.json und catalog.sig committen. Erst dann sehen die Integrationen das Update.
"""
import base64, getpass, hashlib, json, os, pathlib, re, sys, zipfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "Maddog3010/strom-waerme-addons"
PUBLIC_KEY = "Ec624NzE6Y_YndUW4UbMOy3wYiIaNDTa_6URg33a_6Y"   # wie in sponsor.py der Integration
FIELDS = ("id", "n", "name", "short", "icon", "tag", "desc", "version", "build", "stage", "api", "core_min", "needs", "optional", "sponsor", "network")
FIXED = (2026, 1, 1, 0, 0, 0)  # feste Zeit in der ZIP, damit gleiche Dateien die gleiche Prüfsumme ergeben


def b64e(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def notes(addon_dir, version):
    f = addon_dir / "CHANGELOG.md"
    if not f.exists():
        return []
    for sec in re.split(r"^## ", f.read_text(encoding="utf-8"), flags=re.M)[1:]:
        if sec.startswith(f"[{version}]"):
            return [re.sub(r"[*_`]", "", l[2:]).strip() for l in sec.splitlines() if l.startswith("- ")][:8]
    return []


def zip_addon(d, out):
    files = sorted(p for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            zi = zipfile.ZipInfo(f"{d.name}/{p.relative_to(d).as_posix()}", FIXED)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, p.read_bytes())


def key():
    from cryptography.hazmat.primitives import serialization as S
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = os.environ.get("FH_PRIV") or getpass.getpass("Privater Schlüssel (FHPRIV1-…): ")
    k = Ed25519PrivateKey.from_private_bytes(b64d(priv.strip()[len("FHPRIV1-"):]))
    pub = b64e(k.public_key().public_bytes(S.Encoding.Raw, S.PublicFormat.Raw))
    if pub != os.environ.get("FH_PUB", PUBLIC_KEY):
        sys.exit("Der Schlüssel passt nicht zum öffentlichen Schlüssel der Integration.")
    return k


def build():
    dist = ROOT / "dist"; dist.mkdir(exist_ok=True)
    entries = []
    for d in sorted((ROOT / "addons").iterdir()):
        if not (d / "addon.json").exists():
            continue
        m = json.loads((d / "addon.json").read_text(encoding="utf-8"))
        assert m["id"] == d.name, f"{d.name}: id in addon.json passt nicht zum Ordner"
        tag = f"{m['id']}-{m['version']}-b{m.get('build', 1)}"
        out = dist / f"{tag}.zip"
        zip_addon(d, out)
        data = out.read_bytes()
        e = {k: m[k] for k in FIELDS if k in m}
        e.update(url=f"https://github.com/{REPO}/releases/download/{tag}/{tag}.zip", sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                 notes=notes(d, m["version"]), date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        entries.append(e)
        print(f"{tag}: {len(data):,} Bytes · sha256 {e['sha256'][:16]}… · Release-Tag {tag}")
    cat = {"v": 1, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": REPO, "addons": entries}
    raw = (json.dumps(cat, ensure_ascii=False, indent=2) + "\n").encode()
    (ROOT / "catalog.json").write_bytes(raw)
    (ROOT / "catalog.sig").write_text(b64e(key().sign(raw)) + "\n")
    print(f"catalog.json mit {len(entries)} Addon(s) geschrieben und unterschrieben.")


def check():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = (ROOT / "catalog.json").read_bytes()
    Ed25519PublicKey.from_public_bytes(b64d(os.environ.get("FH_PUB", PUBLIC_KEY))).verify(b64d((ROOT / "catalog.sig").read_text().strip()), raw)
    for e in json.loads(raw)["addons"]:
        z = ROOT / "dist" / e["url"].rsplit("/", 1)[1]
        if z.exists():
            assert hashlib.sha256(z.read_bytes()).hexdigest() == e["sha256"], f"{z.name}: Prüfsumme passt nicht"
    print("Katalog und Unterschrift sind in Ordnung.")


if __name__ == "__main__":
    check() if "--check" in sys.argv else build()
