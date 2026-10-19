"""Deterministic PS5 repository generation and explicit Git publication."""

import hashlib
import importlib.util
import io
import json
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from addon_catalogue import IDENTIFIER, VERSION, prepare_manifest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("installed_addons", HERE / "stage-installed-addons.py")
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)
REPOSITORIES = {"stable": "repository.ps5", "testing": "repository.ps5.testing"}
REMOTE = "Notedop/kodi-ps5-binary-addons"


@contextmanager
def workflow_lock(directory):
    if sys.platform != "linux":
        raise ValueError("run PS5 repository build/package/publish commands in Linux/WSL")
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".workflow.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"another repository command is using {directory}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(data):
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def xml_bytes(root):
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def write_changed(path, data, immutable=False):
    if path.is_symlink():
        raise ValueError(f"refusing symlink destination: {path}")
    if path.exists():
        if path.read_bytes() == data:
            return False
        if immutable:
            raise ValueError(f"published version changed: {path}; increment catalogue revision")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
        except OSError:
            temporary.unlink()
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def zip_directory(directory):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(directory.rglob("*")):
            staging.contained(path, directory)
            if not path.is_file() and not path.is_dir():
                continue
            name = (PurePosixPath(directory.name) /
                    PurePosixPath(path.relative_to(directory).as_posix())).as_posix()
            is_directory = path.is_dir()
            if is_directory:
                name += "/"
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            permissions = 0o755 if is_directory or path.stat().st_mode & 0o111 else 0o644
            info.external_attr = ((0o40000 if is_directory else 0o100000) | permissions) << 16
            if is_directory:
                info.external_attr |= 0x10
            archive.writestr(info, b"" if is_directory else path.read_bytes(), compresslevel=9)
    return buffer.getvalue()


def add_release(files, base, folder, provenance=None):
    root = ET.parse(folder / "addon.xml").getroot()
    addon, version = root.get("id"), root.get("version")
    if (root.tag != "addon" or not isinstance(addon, str) or
            not IDENTIFIER.fullmatch(addon) or not isinstance(version, str) or
            not VERSION.fullmatch(version)):
        raise ValueError(f"invalid release manifest: {folder}")
    archive = zip_directory(folder)
    name = base / addon / f"{addon}-{version}.zip"
    files[name] = (archive, True)
    files[Path(str(name) + ".sha256")] = ((digest(archive) + "\n").encode(), True)
    files[base / addon / "addon.xml"] = (xml_bytes(root), False)
    if provenance is not None:
        files[Path(str(name) + ".build.json")] = (json_bytes(provenance), True)
    assets = root.find("./extension[@point='xbmc.addon.metadata']/assets")
    if assets is not None:
        for asset in assets:
            if not asset.text:
                continue
            relative = PurePosixPath(asset.text)
            if relative.is_absolute() or ".." in relative.parts or "\\" in asset.text:
                raise ValueError(f"unsafe asset path in {addon}: {asset.text}")
            source = folder / relative
            staging.contained(source, folder)
            files[base / addon / relative] = (source.read_bytes(), False)
    return root, digest(archive)


def package(root, catalogue, prefixes, output, title, record):
    """Generate both channels; all candidates are checked against the real title."""
    if not title.is_file():
        raise ValueError(f"missing original title ELF: {title}; build ps5-title first")
    title_sha256 = digest(title.read_bytes())
    receipt_path = title.with_name("ps5-addon-runtime.json")
    if not receipt_path.is_file():
        raise ValueError(f"missing PS5 runtime receipt: {receipt_path}; repackage ps5-title first")
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("schema") != 1 or receipt.get("runtime") != catalogue["runtime"] or
            receipt.get("title_sha256") != title_sha256):
        raise ValueError("title runtime receipt does not match the catalogue/original ELF")
    exclusions = json.loads((HERE / "addon-exclusions.json").read_text())
    selected, _ = staging.discover(prefixes, exclusions, catalogue=catalogue)
    found = {item[0] for item in selected}
    missing = set(catalogue["addons"]) - found
    if missing:
        raise ValueError(f"whitelisted add-ons not installed: {', '.join(sorted(missing))}")
    elfs = [str(elf) for _, _, _, binary, helpers in selected for elf in [binary, *helpers]]
    subprocess.run([sys.executable, str(HERE / "verify-addon-exports.py"), str(title), *elfs],
                   check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {}
    runtime_path = Path("repo") / catalogue["runtime"]
    indexes = {channel: ET.Element("addons") for channel in REPOSITORIES}
    manifest = {"schema": 1, "runtime": catalogue["runtime"],
                "title_sha256": title_sha256, "addons": {}}
    with tempfile.TemporaryDirectory(prefix="ps5-repository-", dir=output.parent) as temp:
        temporary = Path(temp)
        staging.stage(selected, temporary)
        for addon in sorted(found):
            folder = temporary / "addons" / addon
            tree = ET.parse(folder / "addon.xml")
            prepare_manifest(tree.getroot(), catalogue, addon)
            (folder / "addon.xml").write_bytes(xml_bytes(tree.getroot()))
            policy = catalogue["addons"][addon]
            target = record["targets"][policy["target"]]
            provenance = {"target": policy["target"], "source_revision": target["source_revision"],
                          "url": target["url"], "fingerprint": target["fingerprint"],
                          "runtime": catalogue["runtime"], "revision": policy["revision"]}
            channels = ["testing"]
            if policy["channel"] == "stable":
                channels.append("stable")
            for channel in channels:
                entry, sha256 = add_release(files, runtime_path / channel, folder, provenance)
                if channel == "stable" and sha256 != policy["accepted_sha256"]:
                    raise ValueError(f"{addon}: stable approval does not match this ZIP SHA256")
                indexes[channel].append(entry)
            manifest["addons"][addon] = dict(provenance, version=tree.getroot().get("version"),
                                             sha256=sha256, channels=channels,
                                             bundled=policy["bundled"])
        for channel, repo_id in REPOSITORIES.items():
            folder = root / "addons" / repo_id
            config = ET.parse(folder / "addon.xml").getroot()
            repository_dir = config.find("./extension[@point='xbmc.addon.repository']/dir")
            if config.get("id") != repo_id or repository_dir is None:
                raise ValueError(f"invalid repository definition: {folder}")
            # Repository URLs and the catalogue must advance together on ABI changes.
            expected = ("https://raw.githubusercontent.com/" + REMOTE +
                        f"/main/repo/{catalogue['runtime']}/{channel}/")
            for key, value in (("info", expected + "addons.xml"),
                               ("checksum", expected + "addons.xml.sha256"),
                               ("datadir", expected)):
                if repository_dir.findtext(key) != value:
                    raise ValueError(f"{repo_id}: {key} does not match catalogue runtime/channel")
            if (repository_dir.find("checksum").get("verify") != "sha256" or
                    repository_dir.findtext("hashes") != "sha256"):
                raise ValueError(f"{repo_id}: repository index and packages must use SHA256")
            entry, _ = add_release(files, runtime_path / channel, folder)
            indexes[channel].append(entry)
            bootstrap = f"{repo_id}-{entry.get('version')}.zip"
            files[Path(bootstrap)] = (zip_directory(folder), True)
            index = xml_bytes(indexes[channel])
            files[runtime_path / channel / "addons.xml"] = (index, False)
            files[runtime_path / channel / "addons.xml.sha256"] = (
                (digest(index) + "\n").encode(), False)
            files[runtime_path / channel / "build-manifest.json"] = (json_bytes(manifest), False)
    files[Path("index.html")] = (
        ('<!doctype html><html><body>\n' + "\n".join(
            f'<a href="{repo}-{ET.parse(root / "addons" / repo / "addon.xml").getroot().get("version")}.zip">'
            f'{repo}</a><br>' for repo in REPOSITORIES.values()) +
         "\n</body></html>\n").encode(), False)
    files[Path("README.md")] = ((
        "# Kodi PS5 binary add-ons\n\n"
        "Generated PS5 packages, not upstream add-on source checkouts. "
        f"Current runtime generation: `{catalogue['runtime']}`.\n\n"
        "Compatible Kodi PS5 builds include the enabled stable repository and the testing "
        "repository, disabled on first discovery. Enable Kodi PS5 Testing Add-ons under "
        "My add-ons > Add-on repository to opt in to unaccepted candidates; the testing "
        "repository ZIP linked by index.html also remains available for manual installation. "
        "Build success and host export validation are not console acceptance. "
        "Packages are not generic FreeBSD binaries.\n\n"
        "## Maintaining this repository\n\n"
        "Use `tools/ps5/addon-repository.py` in the configured PS5 Kodi source checkout; "
        "see `docs/PS5-BINARY-ADDONS.md` there for prerequisites, the nested whitelist, "
        "source locks and stable-promotion evidence.\n\n"
        "1. Run `python3 tools/ps5/addon-repository.py build --jobs 4`.\n"
        "2. Rebuild/package the Kodi title when its candidate exports or runtime change.\n"
        "3. Run `python3 tools/ps5/addon-repository.py package`.\n"
        "4. Run `python3 tools/ps5/addon-repository.py publish --checkout PATH --commit`; "
        "add `--push` only when publication is intended.\n\n"
        "Subsequent build/package runs can use `build --jobs 4 --package`. "
        "Upstream changes require an explicit `build --refresh-sources`. "
        "Never overwrite an existing package version: increment its PS5 revision. "
        "Stable promotion requires console evidence and approval of the exact ZIP SHA256. "
        "Retain matching sources, dependency sources and PS5 patches, and satisfy each "
        "package's license obligations before distribution.\n\n"
        "The generated feeds are under `repo/<runtime>/stable` and "
        "`repo/<runtime>/testing`. Do not run the original template generator over them. "
        "Older package versions are retained. GitHub Pages is optional; repository "
        "definitions use raw GitHub URLs.\n"
    ).encode(), False)
    # Check every immutable artifact before exposing any new index.
    for relative, (data, immutable) in files.items():
        destination = output / relative
        staging.contained(destination, output)
        if destination.is_symlink():
            raise ValueError(f"refusing symlink destination: {destination}")
        if immutable and destination.exists() and destination.read_bytes() != data:
            raise ValueError(f"published version changed: {destination}; increment catalogue revision")
    for relative, (data, immutable) in sorted(files.items(),
                                             key=lambda item: item[0].name.startswith("addons.xml")):
        write_changed(output / relative, data, immutable)
    inventory = {path.as_posix(): digest(data) for path, (data, _) in sorted(files.items())}
    write_changed(output / "publication.json", json_bytes(
        {"schema": 1, "runtime": catalogue["runtime"],
         "catalogue_sha256": digest(json_bytes(catalogue)), "files": inventory}))
    return manifest


def git(checkout, *args):
    return subprocess.check_output(["git", "--no-pager", "-C", str(checkout), *args],
                                   text=True).strip()


def publish(output, checkout, commit=False, push=False, catalogue=None):
    """Copy generated files only; commit and push require explicit opt-in."""
    if push and not commit:
        raise ValueError("--push requires --commit")
    checkout, output = checkout.resolve(), output.resolve()
    if not (checkout / ".git").exists():
        raise ValueError(f"not a Git checkout: {checkout}")
    if checkout == output or checkout in output.parents or output in checkout.parents:
        raise ValueError("publication output and repository checkout must be separate")
    if git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("publication checkout is dirty; commit or remove its changes first")
    if git(checkout, "branch", "--show-current") != "main":
        raise ValueError("repository URLs use main; publication checkout must be on main")
    remote = git(checkout, "remote", "get-url", "origin")
    if remote.removesuffix(".git").rstrip("/") not in (
            f"https://github.com/{REMOTE}", f"git@github.com:{REMOTE}",
            f"ssh://git@github.com/{REMOTE}"):
        raise ValueError(f"unexpected publication remote: {remote}")
    publication = json.loads((output / "publication.json").read_text())
    if publication.get("schema") != 1 or not isinstance(publication.get("files"), dict):
        raise ValueError("invalid publication inventory")
    if catalogue is not None and publication.get("catalogue_sha256") != digest(json_bytes(catalogue)):
        raise ValueError("publication does not match the current catalogue; package again")
    runtime = publication.get("runtime", "")
    if not isinstance(runtime, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", runtime):
        raise ValueError("invalid publication runtime")
    pending = []
    for relative, expected in publication["files"].items():
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative or not path.parts:
            raise ValueError(f"invalid publication path: {relative}")
        if not (relative in ("index.html", "README.md") or
                re.fullmatch(r"repository\.ps5(?:\.testing)?-[A-Za-z0-9.+_@~]+\.zip", relative) or
                (len(path.parts) >= 4 and path.parts[:2] == ("repo", runtime) and
                 path.parts[2] in REPOSITORIES)):
            raise ValueError(f"unexpected publication path: {relative}")
        source, destination = output / path, checkout / path
        staging.contained(source, output)
        staging.contained(destination, checkout)
        data = source.read_bytes()
        if digest(data) != expected:
            raise ValueError(f"publication output modified: {source}; package again")
        immutable = relative.endswith((".zip", ".zip.sha256", ".zip.build.json"))
        if immutable and destination.exists() and destination.read_bytes() != data:
            raise ValueError(f"published version changed: {destination}; increment catalogue revision")
        pending.append((destination, data, immutable))
    for destination, data, immutable in pending:
        write_changed(destination, data, immutable)
    changed = bool(git(checkout, "status", "--porcelain", "--untracked-files=all"))
    if commit and changed:
        git(checkout, "add", "--", *publication["files"])
        git(checkout, "commit", "-m", f"Publish PS5 add-ons ({publication['runtime']})\n\n"
            "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>")
    if push:
        git(checkout, "push", "origin", "HEAD:main")
    return changed
