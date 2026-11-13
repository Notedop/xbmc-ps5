#!/usr/bin/env python3
"""Persistent incremental builder for PS5 binary add-ons."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from native_addon import installed as native_installed
from native_contract import validate_sdk


RUNTIME = "piers-ps5-1"
LOCK_VERSION = 1
RECORD_VERSION = 3
HASH_CACHE_VERSION = 1
DEPENDENCY_CACHE_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


class AddonBuildError(ValueError):
    """An invalid or unsuccessful add-on build operation."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_if_changed(path: Path, data: bytes) -> bool:
    if not path.is_symlink() and path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return True


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_remove(path: Path, workspace: Path) -> None:
    resolved_workspace = workspace.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_workspace or not _inside(resolved_path, resolved_workspace):
        raise AddonBuildError(f"refusing to remove path outside a workspace child: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _run(
    command: list[str], *, env: dict[str, str] | None = None, stream: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=None if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.PIPE,
            env=env,
        )
    except subprocess.CalledProcessError as error:
        for output in (error.stdout, error.stderr):
            if output:
                print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
        raise


def _validate_catalogue(catalogue: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(catalogue, dict) or catalogue.get("schema") != 1:
        raise AddonBuildError("catalogue must be a schema 1 object")
    if catalogue.get("runtime") != RUNTIME:
        raise AddonBuildError(f"catalogue runtime must be {RUNTIME!r}")
    targets = catalogue.get("targets")
    addons = catalogue.get("addons")
    if not isinstance(targets, dict) or not targets or not isinstance(addons, dict) or not addons:
        raise AddonBuildError("catalogue must contain non-empty targets and an addons object")

    for name, target in targets.items():
        if not isinstance(name, str) or not _SAFE_ID.fullmatch(name):
            raise AddonBuildError(f"unsafe target id: {name!r}")
        if not isinstance(target, dict):
            raise AddonBuildError(f"target {name!r} must be an object")
        url, ref, members = target.get("url"), target.get("ref"), target.get("addons")
        if not isinstance(url, str) or not url or not isinstance(ref, str) or not ref:
            raise AddonBuildError(f"target {name!r} requires non-empty url and ref")
        if any(character.isspace() or ord(character) < 32 for character in ref):
            raise AddonBuildError(f"target {name!r} has an invalid git ref")
        _github_archive_url(url, "0" * 40)
        if not isinstance(members, list) or not members:
            raise AddonBuildError(f"target {name!r} requires a non-empty addons list")
        for addon_id in members:
            if not isinstance(addon_id, str) or not _SAFE_ID.fullmatch(addon_id):
                raise AddonBuildError(f"target {name!r} has unsafe addon id: {addon_id!r}")
            if addon_id not in addons:
                raise AddonBuildError(f"target {name!r} references unknown addon {addon_id!r}")
            if addons[addon_id].get("target") != name:
                raise AddonBuildError(f"addon {addon_id!r} does not map back to target {name!r}")
    for addon_id, addon in addons.items():
        if not isinstance(addon_id, str) or not _SAFE_ID.fullmatch(addon_id):
            raise AddonBuildError(f"unsafe addon id: {addon_id!r}")
        if not isinstance(addon, dict) or addon.get("target") not in targets:
            raise AddonBuildError(f"addon {addon_id!r} has an unknown target")
        if addon_id not in targets[addon["target"]]["addons"]:
            raise AddonBuildError(f"addon {addon_id!r} is missing from target membership")
    return targets, addons


def _selected_addons(addons: dict[str, dict[str, Any]], only: list[str] | None) -> list[str]:
    if only:
        target_names = {policy["target"] for policy in addons.values()}
        unknown = sorted(set(only) - set(addons) - target_names)
        if unknown:
            raise AddonBuildError("unknown add-on/target id(s): " + ", ".join(unknown))
        selected_targets = {addons[name]["target"] if name in addons else name for name in only}
        return sorted(name for name, policy in addons.items() if policy["target"] in selected_targets)
    return sorted(addons)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AddonBuildError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AddonBuildError(f"expected a JSON object in {path}")
    return value


def _resolve_ref(url: str, ref: str) -> str:
    if _SHA.fullmatch(ref):
        return ref.lower()
    refs = [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
    result = _run(["git", "ls-remote", url, *refs])
    matches: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] in refs:
            matches[fields[1]] = fields[0].lower()
    for candidate in (refs[2], refs[0], refs[1]):
        if candidate in matches and _SHA.fullmatch(matches[candidate]):
            return matches[candidate]
    raise AddonBuildError(f"could not resolve {ref!r} in {url}")


def _github_archive_url(repository_url: str, commit: str) -> str:
    parsed = urllib.parse.urlsplit(repository_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        raise AddonBuildError(f"only public HTTPS GitHub source URLs are supported: {repository_url}")
    try:
        port = parsed.port
    except ValueError as error:
        raise AddonBuildError(f"invalid GitHub repository URL: {repository_url}") from error
    if port not in (None, 443) or parsed.query or parsed.fragment:
        raise AddonBuildError(f"invalid GitHub repository URL: {repository_url}")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2 or any(part in (".", "..") for part in parts):
        raise AddonBuildError(f"invalid GitHub repository URL: {repository_url}")
    return f"https://github.com/{parts[0]}/{parts[1]}/archive/{commit}.tar.gz"


def _safe_archive_path(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise AddonBuildError(f"unsafe path in add-on source archive: {relative}")
    destination = root.joinpath(*relative.parts)
    if not _inside(destination.resolve(strict=False), root.resolve()):
        raise AddonBuildError(f"archive path escapes extraction directory: {relative}")
    return destination


def _extract_archive_safely(archive_path: Path, destination: Path) -> None:
    """Extract a GitHub tarball without permitting path traversal or special files."""
    root = destination.resolve()
    members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    top_level: set[str] = set()
    seen: set[PurePosixPath] = set()
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            raw = PurePosixPath(member.name)
            if raw.is_absolute() or any(part in ("", ".", "..") for part in raw.parts):
                raise AddonBuildError(f"unsafe path in add-on source archive: {member.name}")
            if len(raw.parts) < 1:
                raise AddonBuildError(f"invalid path in add-on source archive: {member.name}")
            top_level.add(raw.parts[0])
            relative = PurePosixPath(*raw.parts[1:])
            if not relative.parts:
                continue
            if relative in seen:
                raise AddonBuildError(f"duplicate path in add-on source archive: {member.name}")
            seen.add(relative)
            if not (member.isdir() or member.isfile() or member.issym()):
                raise AddonBuildError(f"unsupported special file in add-on source archive: {member.name}")
            if member.islnk():
                raise AddonBuildError(f"hard links are not allowed in add-on source archives: {member.name}")
            members.append((member, relative))
        if len(top_level) != 1:
            raise AddonBuildError("GitHub source archive must contain exactly one top-level directory")

        destination.mkdir(parents=True, exist_ok=False)
        for member, relative in sorted(members, key=lambda item: (len(item[1].parts), item[0].issym())):
            if member.issym():
                continue
            target = _safe_archive_path(root, relative)
            if not _inside(target.parent.resolve(), root):
                raise AddonBuildError(f"archive parent escapes extraction directory: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise AddonBuildError(f"cannot read archive member {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, stat.S_IMODE(member.mode) & 0o777)

        for member, relative in members:
            if not member.issym():
                continue
            link = _safe_archive_path(root, relative)
            link.parent.mkdir(parents=True, exist_ok=True)
            link_target = PurePosixPath(member.linkname)
            if link_target.is_absolute():
                raise AddonBuildError(f"absolute symlink in add-on source archive: {member.name}")
            resolved_target = (link.parent / Path(*link_target.parts)).resolve(strict=False)
            if not _inside(resolved_target, root):
                raise AddonBuildError(f"symlink escapes extraction directory: {member.name}")
            os.symlink(member.linkname, link)
            if not _inside(link.resolve(strict=False), root):
                raise AddonBuildError(f"symlink escapes extraction directory: {member.name}")


def _fetch_source(
    workspace: Path, target_name: str, repository_url: str, commit: str
) -> Path:
    url = _github_archive_url(repository_url, commit)
    cache_key = hashlib.sha256(f"{repository_url}\0{commit}".encode()).hexdigest()
    downloads = workspace / "downloads"
    sources = workspace / "sources" / target_name
    archive_path = downloads / f"{cache_key}.tar.gz"
    source_path = sources / commit
    marker_path = sources / f"{commit}.json"
    marker = {"url": repository_url, "commit": commit}
    if _source_cache_valid(source_path, {"url": repository_url, "sha": commit}):
        return source_path

    downloads.mkdir(parents=True, exist_ok=True)
    if archive_path.is_symlink():
        raise AddonBuildError(f"refusing to use a symlink as a cached source archive: {archive_path}")
    if not archive_path.is_file():
        fd, temporary = tempfile.mkstemp(prefix=f".{cache_key}.", dir=downloads)
        try:
            with os.fdopen(fd, "wb") as output:
                with urllib.request.urlopen(url, timeout=90) as response:
                    final_url = urllib.parse.urlsplit(response.geturl())
                    if final_url.scheme != "https" or final_url.hostname not in (
                        "github.com",
                        "codeload.github.com",
                    ):
                        raise AddonBuildError(
                            f"source archive redirected outside GitHub HTTPS: {response.geturl()}"
                        )
                    shutil.copyfileobj(response, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, archive_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    sources.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{commit}.", dir=sources) as temporary_dir:
        extracted = Path(temporary_dir) / "source"
        _extract_archive_safely(archive_path, extracted)
        if source_path.exists():
            _safe_remove(source_path, workspace)
        os.replace(extracted, source_path)
        _write_if_changed(marker_path, _json_bytes(marker))
    return source_path


def _file_signature(path: Path) -> list[Any]:
    link_stat = path.lstat()
    link_target = os.readlink(path) if path.is_symlink() else None
    try:
        content_stat = path.stat()
    except OSError:
        content_stat = link_stat
    return [
        link_stat.st_mode,
        link_stat.st_size,
        link_stat.st_mtime_ns,
        link_stat.st_ctime_ns,
        link_stat.st_ino,
        link_target,
        content_stat.st_mode,
        content_stat.st_size,
        content_stat.st_mtime_ns,
        content_stat.st_ctime_ns,
        content_stat.st_ino,
    ]


class _InputHasher:
    def __init__(self, workspace: Path):
        self.path = workspace / "input-hash-cache.json"
        cache = _read_json(self.path, {"schema": HASH_CACHE_VERSION, "files": {}})
        if cache.get("schema") != HASH_CACHE_VERSION or not isinstance(cache.get("files"), dict):
            raise AddonBuildError(f"unsupported input hash cache: {self.path}")
        self.files: dict[str, dict[str, Any]] = cache["files"]
        self.changed = False

    def _hash_file(self, path: Path) -> str:
        key = str(path.absolute())
        signature = _file_signature(path)
        cached = self.files.get(key)
        if cached and cached.get("signature") == signature and isinstance(cached.get("sha256"), str):
            return cached["sha256"]
        digest = hashlib.sha256()
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            if not path.exists():
                result = digest.hexdigest()
            elif path.is_file():
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                result = digest.hexdigest()
            else:
                result = digest.hexdigest()
        else:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result = digest.hexdigest()
        self.files[key] = {"signature": signature, "sha256": result}
        self.changed = True
        return result

    def file_digest(self, path: Path) -> str:
        return self._hash_file(path)

    def tree_digest(
        self, paths: Iterable[Path], exclude: Iterable[Path] | None = None
    ) -> str:
        digest = hashlib.sha256()
        resolved_excludes = tuple(path.resolve() for path in exclude or ())

        def excluded(path: Path) -> bool:
            resolved = path.resolve(strict=False)
            return any(_inside(resolved, excluded_root) for excluded_root in resolved_excludes)

        files: set[Path] = set()
        for root in paths:
            if not root.exists() and not root.is_symlink():
                raise AddonBuildError(f"required build input does not exist: {root}")
            if root.is_file() or root.is_symlink():
                files.add(root)
                continue
            for current, directories, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                directories[:] = sorted(
                    directory
                    for directory in directories
                    if not excluded(current_path / directory)
                )
                for directory in list(directories):
                    entry = current_path / directory
                    if entry.is_symlink():
                        files.add(entry)
                        directories.remove(directory)
                for filename in filenames:
                    entry = current_path / filename
                    if excluded(entry):
                        continue
                    files.add(entry)
        for path in sorted(files, key=lambda item: str(item)):
            if excluded(path):
                continue
            relative = str(path.absolute())
            file_hash = self._hash_file(path)
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(file_hash.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def save(self) -> None:
        if self.changed:
            _write_if_changed(
                self.path,
                _json_bytes({"schema": HASH_CACHE_VERSION, "files": self.files}),
            )


def _cmake_escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise AddonBuildError(f"unsupported newline in CMake toolchain value: {value!r}")
    return value.replace("\\", "/").replace('"', '\\"').replace("$", "\\$")


def _cmake_quote(value: str) -> str:
    return '"' + _cmake_escape(value) + '"'


def _sdk_inputs(sdk: Path) -> list[Path]:
    candidates = [
        sdk / "bin" / name
        for name in (
            "prospero-clang",
            "prospero-clang++",
            "prospero-llvm-config",
            "prospero-ar",
            "prospero-lld",
            "prospero-nm",
            "prospero-ranlib",
            "prospero-nm",
            "prospero-ranlib",
        )
    ]
    candidates.append(sdk / "bin")
    candidates.extend(
        sdk / relative
        for relative in (
            "target/include",
            "target/include_common",
            "target/lib",
            "target/lib_common",
            "target/user/homebrew",
            "sysroot/usr/include",
            "sysroot/usr/lib",
            "lib/clang",
            "llvm/bin",
            "lib/llvm/bin",
            "target/bin",
            "usr/bin",
        )
    )
    return [path for path in candidates if path.exists() or path.is_symlink()]


def _native_root(root: Path, depends: Path) -> Path | None:
    for candidate in sorted(depends.parent.glob("*-native")):
        if candidate.is_dir():
            return candidate
    site = root / "tools" / "depends" / "target" / "config-binaddons.site"
    if site.is_file():
        match = re.search(r"(?m)^NATIVE_ROOT=(.+?)\s*$", site.read_text(encoding="utf-8"))
        if match:
            configured = Path(match.group(1).strip('"\''))
            if configured.is_dir():
                return configured
    return None


def _autoconf_files(root: Path, depends: Path) -> list[Path]:
    native = _native_root(root, depends)
    for candidate in ([native] if native else []):
        matches = sorted(candidate.glob("share/automake-*/config.sub"))
        for config_sub in matches:
            config_guess = config_sub.with_name("config.guess")
            if config_guess.is_file():
                return [config_sub, config_guess]
    return []


def _pkg_config_tool(root: Path, depends: Path) -> str:
    native = _native_root(root, depends)
    candidate = native / "bin" / "pkg-config" if native else None
    if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    executable = shutil.which("pkg-config")
    if not executable:
        raise AddonBuildError("pkg-config is missing from the configured native tools and host PATH")
    return executable


def _prepare_sdk_pkgconfig(sdk: Path, destination: Path) -> tuple[list[Path], list[Path]]:
    homebrew = sdk / "target" / "user" / "homebrew"
    patched_root = destination / "sdk-pkgconfig-patched"
    lib_dirs: list[Path] = []
    path_dirs: list[Path] = []
    for source_relative, patched_relative, is_libdir in (
        ("lib/pkgconfig", "lib/pkgconfig", True),
        ("libdata/pkgconfig", "libdata/pkgconfig", False),
    ):
        source_dir = homebrew / source_relative
        if not source_dir.is_dir():
            continue
        patched_dir = patched_root / patched_relative
        patched_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_dir.glob("*.pc")):
            content = source.read_text(encoding="utf-8")
            patched = content.replace("/user/homebrew", str(homebrew))
            _write_if_changed(patched_dir / source.name, patched.encode("utf-8"))
        (lib_dirs if is_libdir else path_dirs).append(patched_dir)
    return lib_dirs, path_dirs


def _autoconf_site(sdk: Path, runtime_sdk: Path) -> str:
    lines = ["cross_compiling=yes", "host=x86_64-unknown-freebsd",
             "host_alias=x86_64-unknown-freebsd"]
    for variable, tool in (
        ("CC", "clang"), ("CXX", "clang++"), ("CPP", "clang"),
        ("AR", "ar"), ("LD", "lld"), ("NM", "nm"), ("RANLIB", "ranlib"),
        ("STRIP", "strip"),
    ):
        command = [str(sdk / "bin" / f"prospero-{tool}")]
        if variable in ("CC", "CXX", "CPP"):
            command = [sys.executable, str(runtime_sdk / "tools/native-clang.py"), *command,
                       "-fPIC", "-femulated-tls", "-fno-stack-protector",
                       "-include", str(runtime_sdk / "include/ps5_native_abi.h")]
            if variable == "CXX":
                command += ["-nostdinc++", "-I" + str(sdk / "target/include/c++/v1")]
            command += ["-I" + str(runtime_sdk / "include")]
            if variable == "CPP":
                command.append("-E")
        lines.append(f"{variable}={shlex.quote(shlex.join(command))}")
    return "\n".join(lines) + "\n"


def _validate_environment(root: Path, sdk: Path, depends: Path) -> None:
    required_sdk_tools = (
        "prospero-clang",
        "prospero-clang++",
        "prospero-llvm-config",
        "prospero-ar",
        "prospero-lld",
    )
    missing = [
        str(sdk / "bin" / tool)
        for tool in required_sdk_tools
        if not (sdk / "bin" / tool).is_file() or not os.access(sdk / "bin" / tool, os.X_OK)
    ]
    for relative in ("target/include", "target/include_common"):
        if not (sdk / relative).is_dir():
            missing.append(str(sdk / relative))
    if missing:
        raise AddonBuildError("PS5 SDK is missing required files: " + ", ".join(missing))
    configured_content = any(
        (depends / item).is_dir() and next((depends / item).iterdir(), None) is not None
        for item in ("include", "lib", "share")
    )
    if not depends.is_dir() or not configured_content:
        raise AddonBuildError(
            f"configured PS5 depends prefix is missing or empty: {depends}; "
            "configure/build tools/depends first"
        )
    if not (root / "tools/ps5/toolchain" / "ps5-native-addon.cmake").is_file():
        raise AddonBuildError("missing tools/ps5/toolchain/ps5-native-addon.cmake")
    if not (root / "cmake" / "addons" / "CMakeLists.txt").is_file():
        raise AddonBuildError("missing cmake/addons/CMakeLists.txt")
    if not shutil.which("cmake"):
        raise AddonBuildError("cmake is missing from PATH")
    if not shutil.which("ninja"):
        raise AddonBuildError("ninja is missing from PATH")
    _pkg_config_tool(root, depends)


def _resolve_sources(
    workspace: Path,
    targets: dict[str, dict[str, Any]],
    refresh: bool,
) -> dict[str, dict[str, str]]:
    lock_path = workspace / "source-lock.json"
    lock = _read_json(lock_path, {"schema": LOCK_VERSION, "targets": {}})
    if lock.get("schema") != LOCK_VERSION or not isinstance(lock.get("targets"), dict):
        raise AddonBuildError(f"unsupported source lock: {lock_path}")
    entries = dict(lock["targets"])
    resolved: dict[str, dict[str, str]] = {}
    changed = False
    for name, target in sorted(targets.items()):
        url, ref = target["url"], target["ref"]
        pinned = entries.get(name)
        if (
            not refresh
            and isinstance(pinned, dict)
            and pinned.get("url") == url
            and pinned.get("ref") == ref
            and isinstance(pinned.get("sha"), str)
            and _SHA.fullmatch(pinned["sha"])
        ):
            commit = pinned["sha"].lower()
        else:
            commit = _resolve_ref(url, ref)
            entries[name] = {"url": url, "ref": ref, "sha": commit}
            changed = True
        resolved[name] = {"url": url, "ref": ref, "sha": commit}
    if changed:
        _write_if_changed(lock_path, _json_bytes({"schema": LOCK_VERSION, "targets": entries}))
    return resolved


def _locked_sources(
    workspace: Path, targets: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str]]:
    lock_path = workspace / "source-lock.json"
    lock = _read_json(lock_path, {"schema": LOCK_VERSION, "targets": {}})
    if lock.get("schema") != LOCK_VERSION or not isinstance(lock.get("targets"), dict):
        raise AddonBuildError(f"unsupported source lock: {lock_path}")
    resolved: dict[str, dict[str, str]] = {}
    for name, target in sorted(targets.items()):
        pinned = lock["targets"].get(name)
        if (
            not isinstance(pinned, dict)
            or pinned.get("url") != target["url"]
            or pinned.get("ref") != target["ref"]
            or not isinstance(pinned.get("sha"), str)
            or not _SHA.fullmatch(pinned["sha"])
        ):
            raise AddonBuildError(
                f"source pin for {name} is missing or stale; run the add-on builder first"
            )
        resolved[name] = {
            "url": target["url"],
            "ref": target["ref"],
            "sha": pinned["sha"].lower(),
        }
    return resolved


def _locate_addon_source(source_root: Path, addon_id: str, target_name: str, member_count: int) -> Path:
    if (addon_id == target_name or member_count == 1) and (source_root / "CMakeLists.txt").is_file():
        return source_root
    candidates = (
        source_root / addon_id,
        source_root / "addons" / addon_id,
        source_root / "addon" / addon_id,
    )
    for candidate in candidates:
        if (candidate / "CMakeLists.txt").is_file():
            return candidate
    raise AddonBuildError(
        f"cannot locate CMake build root for add-on {addon_id!r} in source repository {source_root}"
    )


def _stage_sources(
    source_root: Path,
    source_prefix: Path,
    addon_ids: list[str],
    target_name: str,
    target_members: list[str],
    workspace: Path,
) -> None:
    _safe_remove(source_prefix, workspace)
    source_prefix.mkdir(parents=True)
    source_root_resolved = source_root.resolve()
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        for name in (*directories, *filenames):
            entry = Path(current) / name
            if entry.is_symlink() and not _inside(entry.resolve(strict=False), source_root_resolved):
                raise AddonBuildError(f"add-on source symlink escapes its repository: {entry}")
    for addon_id in addon_ids:
        definition = _locate_addon_source(source_root, addon_id, target_name, len(target_members))
        if not _inside(definition.resolve(), source_root_resolved):
            raise AddonBuildError(f"add-on source directory escapes its repository: {definition}")
        destination = source_prefix / addon_id
        shutil.copytree(definition, destination, symlinks=False)


def _cmake_inputs(
    root: Path, addon_ids: list[str], sdk: Path, depends: Path,
    runtime_sdk: Path | None = None, archive_map: Path | None = None,
) -> list[Path]:
    ccache = shutil.which("ccache")
    pkg_config = _pkg_config_tool(root, depends)
    inputs = [
        root / "tools" / "ps5" / "scripts" / "addon_build.py",
        root / "tools" / "ps5" / "scripts" / "native_addon.py",
        root / "tools" / "ps5" / "scripts" / "native_contract.py",
        root / "tools/ps5/toolchain" / "ps5-native-addon.cmake",
        root / "cmake" / "scripts" / "ps5",
        archive_map,
        root / "cmake" / "addons" / "CMakeLists.txt",
        root / "cmake" / "KodiConfig.cmake.in",
        root / "cmake" / "addons" / "depends",
        root / "cmake" / "scripts" / "common",
        root / "cmake" / "modules",
        root / "cmake" / "platform" / "freebsd",
        root / "cmake" / "platform" / "ps5",
        root / "tools" / "depends" / "target" / "config-binaddons.site",
        root / "tools" / "depends" / "xbmc-addons.include",
        root / "tools" / "depends" / "Makefile.include",
        root / "xbmc" / "addons" / "kodi-dev-kit" / "include",
        *_autoconf_files(root, depends),
        root / "tools" / "ps5" / "scripts" / "apply-adaptive-patch.cmake",
        root / "tools" / "ps5" / "scripts" / "apply-patch-series.cmake",
        root / "tools" / "ps5" / "scripts" / "apply-source-patch.cmake",
        root / "tools" / "ps5" / "patches" / "inputstream-adaptive-ps5.patch",
        depends,
        *_sdk_inputs(sdk),
        Path(ccache) if ccache else None,
        Path(pkg_config) if Path(pkg_config).is_absolute() else None,
        shutil.which("cmake") and Path(shutil.which("cmake") or ""),
        shutil.which("ninja") and Path(shutil.which("ninja") or ""),
    ]
    if runtime_sdk is not None:
        receipt = runtime_sdk / "receipt.json"
        inputs.append(receipt)
        inputs.extend(runtime_sdk / relative
                      for relative in json.loads(receipt.read_text())["files"])
    for addon_id in addon_ids:
        patch_dir = root / "tools" / "ps5" / "patches" / "addons" / addon_id
        if patch_dir.exists():
            inputs.append(patch_dir)
    if archive_map is not None:
        for replacement in json.loads(archive_map.read_text()).values():
            path = Path(replacement)
            inputs.extend((path, path.parent / "receipt.json"))
    return [path for path in inputs if path and (path.exists() or path.is_symlink())]


def _expected_installs(prefix: Path, addon_ids: list[str]) -> list[Path]:
    found = {}
    for manifest in sorted((prefix / "share/kodi/addons").glob("*/addon.xml")):
        addon_id = ET.parse(manifest).getroot().get("id")
        if addon_id not in addon_ids:
            continue
        if addon_id in found:
            raise AddonBuildError(f"duplicate installed add-on ID: {addon_id}")
        _validate_install(manifest.parent, prefix)
        found[addon_id] = manifest.parent
    missing = set(addon_ids) - set(found)
    if missing:
        raise AddonBuildError(f"installed add-on metadata missing under {prefix}: "
                              + ", ".join(sorted(missing)))
    return sorted(found.values())


def _validate_install(directory: Path, prefix: Path) -> None:
    try:
        native_installed(directory, prefix)
    except ValueError as error:
        raise AddonBuildError(str(error)) from error


def _install_artifacts(prefix: Path, directories: list[Path]) -> list[Path]:
    artifacts = []
    for directory in directories:
        binary_directory = prefix / "lib/kodi/addons" / directory.name
        for folder in (directory, binary_directory):
            if not _inside(folder.resolve(), prefix.resolve()):
                raise AddonBuildError(f"installed add-on directory escapes its prefix: {folder}")
            for artifact in folder.rglob("*"):
                if not _inside(artifact.resolve(), folder.resolve()):
                    raise AddonBuildError(f"installed add-on artifact escapes its directory: {artifact}")
                if artifact.is_file():
                    artifacts.append(artifact)
    return sorted(artifacts)


def _record_entry_valid(
    entry: Any,
    target_name: str,
    addon_ids: list[str],
    fingerprint: str,
    source: dict[str, str],
    workspace: Path,
    staged_source_digest: str | None,
    hasher: _InputHasher,
) -> list[Path] | None:
    if not isinstance(entry, dict):
        return None
    if (
        entry.get("fingerprint") != fingerprint
        or entry.get("url") != source["url"]
        or entry.get("source_revision") != source["sha"]
    ):
        return None
    if entry.get("target") != target_name or entry.get("addons") != addon_ids:
        return None
    if entry.get("staged_source_digest") != staged_source_digest or staged_source_digest is None:
        return None
    prefix_value = entry.get("prefix")
    if not isinstance(prefix_value, str):
        return None
    install_prefix = Path(prefix_value)
    if not install_prefix.is_absolute() or not _inside(install_prefix.resolve(strict=False), workspace.resolve()):
        return None
    relative_paths = entry.get("installed", [])
    if not isinstance(relative_paths, list) or not relative_paths:
        return None
    expected_artifacts = entry.get("artifacts")
    if not isinstance(expected_artifacts, dict) or not expected_artifacts:
        return None
    paths = []
    for relative in relative_paths:
        if not isinstance(relative, str):
            return None
        path = (install_prefix / relative).resolve(strict=False)
        if not _inside(path, install_prefix.resolve()):
            return None
        try:
            _validate_install(path, install_prefix)
        except (AddonBuildError, ET.ParseError):
            return None
        paths.append(path)
    if {str(path.relative_to(install_prefix)) for path in _install_artifacts(install_prefix, paths)} != set(expected_artifacts):
        return None
    for relative, expected_hash in expected_artifacts.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            return None
        artifact = install_prefix / relative
        if not _inside(artifact.resolve(strict=False), install_prefix.resolve()) or not artifact.is_file():
            return None
        digest = hasher.file_digest(artifact)
        if digest != expected_hash:
            return None
    return paths


def _source_cache_valid(source_root: Path, source: dict[str, str]) -> bool:
    if source_root.is_symlink() or not source_root.is_dir():
        return False
    marker = source_root.parent / f"{source_root.name}.json"
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return value == {"url": source["url"], "commit": source["sha"]}


def _target_fingerprint(
    hasher: _InputHasher,
    root: Path,
    target_name: str,
    addon_ids: list[str],
    source: dict[str, str],
    sdk: Path,
    depends: Path,
    workspace: Path,
    source_root: Path,
    runtime_sdk: Path | None = None,
    archive_map: Path | None = None,
) -> str:
    workspace_excludes = [
        workspace / "build",
        workspace / "install",
        workspace / "downloads",
        workspace / "sources",
        workspace / "source-lock.json",
        workspace / "build-record.json",
        workspace / "input-hash-cache.json",
    ]
    cmake_digest = hasher.tree_digest(
        _cmake_inputs(root, sorted(set([target_name, *addon_ids])),
                      sdk, depends, runtime_sdk, archive_map),
        exclude=workspace_excludes,
    )
    values = {
        "target": target_name,
        "addons": addon_ids,
        "url": source["url"],
        "ref": source["ref"],
        "source_sha": source["sha"],
        "source_content": hasher.tree_digest([source_root]),
        "cmake_inputs": cmake_digest,
        "sdk": str(sdk.resolve()),
        "depends": str(depends.resolve()),
        "build_type": "Release",
        "generator": "Ninja",
        "override_paths": True,
    }
    return hashlib.sha256(_json_bytes(values)).hexdigest()


def _refresh_prefixes(record: dict[str, Any]) -> None:
    prefixes = set()
    for entry in record.get("targets", {}).values():
        if not isinstance(entry, dict):
            continue
        prefix = entry.get("prefix")
        if not isinstance(prefix, str):
            continue
        prefix_path = Path(prefix)
        if prefix_path.is_absolute() and prefix_path.is_dir():
            prefixes.add(str(prefix_path.resolve()))
    record["prefixes"] = sorted(prefixes)


def _dependency_context(
    hasher: _InputHasher, root: Path, target_name: str, addon_ids: list[str],
    sdk: Path, depends: Path, workspace: Path, runtime_sdk: Path, archive_map: Path,
) -> dict[str, Any]:
    inputs = _cmake_inputs(root, sorted(set([target_name, *addon_ids])),
                           sdk, depends, runtime_sdk, archive_map)
    # Orchestration changes are qualified by generated commands and this schema,
    # rather than invalidating every dependency for a cache-management edit.
    excludes = [
        root / "tools/ps5/scripts/addon_build.py",
        root / "cmake/addons/CMakeLists.txt",
        root / "cmake/scripts/common/HandleDepends.cmake",
        workspace,
        *(root / "tools/ps5/patches/addons" / name
          for name in sorted(set([target_name, *addon_ids]))),
    ]
    environment = {
        name: os.environ.get(name) for name in (
            "PATH", "CC", "CXX", "CCAS", "AR", "AS", "NM", "RANLIB",
            "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LIBRARY_PATH",
            "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "SOURCE_DATE_EPOCH",
        )
    }
    return {
        "schema": DEPENDENCY_CACHE_VERSION,
        "shared_inputs": hasher.tree_digest(inputs, exclude=excludes),
        "native_site": _autoconf_site(sdk, runtime_sdk),
        "environment": environment,
    }


def _dependency_files(prefix: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(prefix.rglob("*")):
        if path.is_symlink():
            if not _inside(path.resolve(), prefix.resolve()):
                raise AddonBuildError(f"dependency output symlink escapes its prefix: {path}")
            files[str(path.relative_to(prefix))] = {"link": os.readlink(path)}
        elif path.is_file():
            files[str(path.relative_to(prefix))] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
    return files


def _preserve_build_diagnostics(path: Path, workspace: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.resolve() == workspace.resolve() or not _inside(path.resolve(), workspace.resolve()):
        raise AddonBuildError(f"refusing to move unqualified build directory: {path}")
    destination = Path(tempfile.mkdtemp(prefix=f"{path.name}-invalid-", dir=path.parent))
    os.replace(path, destination)


class _DependencyBuild:
    def work_digest(self, path: Path) -> str | None:
        return self.hasher.tree_digest([path]) if path.is_dir() else None

    def __init__(
        self, workspace: Path, target_name: str, fingerprint: str,
        context: dict[str, Any], hasher: _InputHasher,
    ) -> None:
        self.workspace = workspace
        self.hasher = hasher
        self.state_path = workspace / "build-state" / f"{target_name}.json"
        self.state = _read_json(self.state_path, {})
        self.legacy_recipes: dict[str, str] = {}
        self.context = context
        self.environment = context["environment"]
        if self.state and self.state.get("context") != context:
            print("  Dependency build inputs changed; selecting a fresh workspace.", flush=True)
            self.state = {}
        if not self.state:
            proof = _read_json(workspace / "legacy-resume-inputs.json", {})
            if (proof.get("schema") == 1 and proof.get("target") == target_name
                    and proof.get("shared_inputs") == context["shared_inputs"]):
                candidate = Path(proof["build_dir"])
                if (candidate.is_absolute() and _inside(candidate.resolve(), workspace.resolve())
                        and self.work_digest(candidate / "source-prefix")
                        == proof.get("staged_source_digest")
                        and (candidate / "toolchain/config.site").read_text()
                        == context["native_site"]):
                    for directory in (candidate / "build").iterdir():
                        name = directory.name
                        done = directory / "src" / f"{name}-stamp" / f"{name}-done"
                        recipe = candidate / "source-prefix" / target_name / "depends/common" / name
                        if done.is_file() and recipe.is_dir():
                            self.legacy_recipes[name] = hasher.tree_digest([recipe])
                    self.state = {
                        "context": context, "fingerprint": proof["fingerprint"],
                        "build_dir": str(candidate), "install_prefix": proof["install_prefix"],
                        "staged_source_digest": proof["staged_source_digest"], "dependencies": {},
                    }
                    print("  Adopting the explicitly input-qualified failed build.", flush=True)
        if self.state:
            self.build_dir = Path(self.state["build_dir"])
            self.install_prefix = Path(self.state["install_prefix"])
            for path in (self.build_dir, self.install_prefix):
                if not path.is_absolute() or not _inside(path.resolve(), workspace.resolve()):
                    raise AddonBuildError(f"dependency build state escapes workspace: {path}")
            if self.work_digest(self.build_dir / "source-prefix") != self.state.get("staged_source_digest"):
                print("  Staged sources changed outside the builder; refusing dependency reuse.", flush=True)
                self.state = {}
                self.legacy_recipes = {}
        if not self.state:
            parent = workspace / "build" / target_name
            self.build_dir = parent / fingerprint
            if self.build_dir.exists():
                _preserve_build_diagnostics(self.build_dir, workspace)
                print("  Preserved unqualified build diagnostics before rebuilding.", flush=True)
            self.install_prefix = workspace / "install" / target_name / self.build_dir.name
            self.state = {
                "context": context, "build_dir": str(self.build_dir),
                "install_prefix": str(self.install_prefix), "dependencies": {},
            }
        self.changed = self.state.get("fingerprint") != fingerprint
        self.state["fingerprint"] = fingerprint
        self.prefix = self.build_dir / "build/depends"
        self.plan_path = self.build_dir / "dependency-plan.tsv"
        self.plan: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []

    def save(self) -> None:
        self.state["staged_source_digest"] = self.hasher.tree_digest([self.build_dir / "source-prefix"])
        _write_if_changed(self.state_path, _json_bytes(self.state))

    def snapshot(self, files: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        parent = self.workspace / "dependency-cache"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=parent))
        destination = parent / temporary.name.removeprefix(".")
        try:
            for relative, metadata in files.items():
                source = self.prefix / relative
                output = temporary / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                if "link" in metadata:
                    output.symlink_to(metadata["link"])
                else:
                    shutil.copy2(source, output)
            current = _dependency_files(self.prefix)
            if any(current.get(relative) != metadata for relative, metadata in files.items()):
                raise AddonBuildError("dependency outputs changed while recording their cache")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                _safe_remove(temporary, self.workspace)
        return {"directory": str(destination), "files": files}

    def cache_valid(self, entry: dict[str, Any], *, check_live: bool = True) -> bool:
        snapshot = entry.get("snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("files"), dict):
            return False
        directory = Path(snapshot.get("directory", ""))
        if (not directory.is_absolute()
                or directory.is_symlink() or not directory.is_dir()
                or not _inside(directory.resolve(), (self.workspace / "dependency-cache").resolve())):
            return False
        for relative, metadata in snapshot["files"].items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                return False
            path = directory / relative
            live = self.prefix / relative
            if not _inside(path.absolute(), directory) or not _inside(live.absolute(), self.prefix):
                return False
            if "link" in metadata:
                if not path.is_symlink() or os.readlink(path) != metadata["link"]:
                    return False
                if check_live and (not live.is_symlink() or os.readlink(live) != metadata["link"]):
                    return False
                target = live.parent / metadata["link"]
                if not _inside(target.resolve(), self.prefix.resolve()):
                    return False
            else:
                candidates = [(path, directory)]
                if check_live:
                    candidates.append((live, self.prefix))
                for candidate, boundary in candidates:
                    if (candidate.is_symlink() or not candidate.is_file()
                            or not _inside(candidate.resolve(), boundary.resolve())
                            or hashlib.sha256(candidate.read_bytes()).hexdigest() != metadata["sha256"]
                            or stat.S_IMODE(candidate.stat().st_mode) != metadata["mode"]):
                        return False
        return True

    def restore(self) -> None:
        _safe_remove(self.prefix, self.workspace)
        self.prefix.mkdir(parents=True)
        snapshots = [self.state["baseline"]]
        snapshots.extend(self.state["dependencies"][name]["snapshot"]
                         for name in self.order if name in self.state["dependencies"])
        for snapshot in snapshots:
            directory = Path(snapshot["directory"])
            for relative, metadata in snapshot["files"].items():
                destination = self.prefix / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                if "link" in metadata:
                    destination.symlink_to(metadata["link"])
                else:
                    shutil.copy2(directory / relative, destination)

    def prepare(self) -> bool:
        generated = [
            self.build_dir / name for name in ("toolchain", "host-tools", "sdk-pkgconfig-patched")
            if (self.build_dir / name).is_dir()
        ]
        generated_digest = self.hasher.tree_digest(generated)
        for line in self.plan_path.read_text().splitlines():
            name, recipe, dependencies, prefix, commands, pinned = line.split("\t")
            if not _SAFE_ID.fullmatch(name):
                raise AddonBuildError(f"unsafe dependency id in build plan: {name}")
            build_prefix = Path(prefix)
            if not _inside(build_prefix.resolve(), self.build_dir.resolve()) or build_prefix == self.build_dir:
                raise AddonBuildError(f"dependency prefix escapes build directory: {prefix}")
            self.plan[name] = {
                "recipe": self.hasher.tree_digest([Path(recipe)]),
                "dependencies": [dep for dep in dependencies.split(";") if dep],
                "prefix": prefix, "commands": commands, "pinned": pinned == "TRUE",
            }
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in self.order:
                return
            if name in visiting or name not in self.plan:
                raise AddonBuildError(f"cyclic or unknown dependency in build plan: {name}")
            visiting.add(name)
            node = self.plan[name]
            for dependency in node["dependencies"]:
                visit(dependency)
            node["key"] = hashlib.sha256(_json_bytes({
                "context": self.context, "prefix": str(self.prefix),
                "environment": self.environment, "generated_inputs": generated_digest,
                "recipe": node["recipe"], "commands": node["commands"],
                "dependencies": {dep: self.plan[dep]["key"] for dep in node["dependencies"]},
            })).hexdigest()
            visiting.remove(name)
            self.order.append(name)

        for name in self.plan:
            visit(name)
        entries = self.state["dependencies"]
        if self.legacy_recipes:
            if any(name not in self.plan or self.plan[name]["recipe"] != digest
                   or not self.plan[name]["pinned"]
                   for name, digest in self.legacy_recipes.items()):
                raise AddonBuildError("legacy dependency recipes do not qualify for reuse")
            baseline = self.snapshot(_dependency_files(self.prefix),
                                     hashlib.sha256((str(self.prefix) + "-legacy").encode()).hexdigest())
            self.state["baseline"] = baseline
            self.state["legacy"] = {name: self.plan[name]["key"] for name in self.legacy_recipes}
            for name in self.legacy_recipes:
                entries[name] = {
                    "key": self.plan[name]["key"], "snapshot": {"directory": baseline["directory"], "files": {}},
                    "work_digest": self.work_digest(Path(self.plan[name]["prefix"])),
                }
        if "baseline" not in self.state:
            self.state["baseline"] = self.snapshot(_dependency_files(self.prefix),
                                                  hashlib.sha256((str(self.prefix) + "-baseline").encode()).hexdigest())
        legacy = self.state.get("legacy", {})
        reset_baseline = (
            not self.cache_valid({"snapshot": self.state["baseline"]}, check_live=False)
            or any(name not in self.plan or self.plan[name]["key"] != key for name, key in legacy.items())
        )
        invalid: set[str] = set(entries) - set(self.plan)
        expected = dict(self.state["baseline"]["files"])
        owners: dict[str, str | None] = dict.fromkeys(expected)
        for name in self.order:
            if name in entries:
                expected.update(entries[name]["snapshot"]["files"])
                owners.update(dict.fromkeys(entries[name]["snapshot"]["files"], name))
        current = _dependency_files(self.prefix)
        for relative, metadata in expected.items():
            if current.get(relative) != metadata:
                owner = owners[relative]
                if owner is None:
                    reset_baseline = True
                else:
                    invalid.add(owner)
        for name in self.order:
            node = self.plan[name]
            entry = entries.get(name)
            if (entry is not None and (
                    not node["pinned"] or entry.get("key") != node["key"]
                    or not self.cache_valid(entry, check_live=False)
                    or entry.get("work_digest") != self.work_digest(Path(node["prefix"]))
                    or any(dep in invalid for dep in node["dependencies"]))):
                invalid.add(name)
        pending = self.state.get("pending")
        if isinstance(pending, dict):
            name = pending["name"]
            if (name not in self.plan or pending["key"] != self.plan[name]["key"]
                    or pending["work_digest"] != self.work_digest(Path(self.plan[name]["prefix"]))):
                invalid.add(name)
        for name in self.order:
            if (name not in entries and Path(self.plan[name]["prefix"]).exists()
                    and (not pending or pending["name"] != name or name in invalid)):
                invalid.add(name)
        for name in self.order:
            if any(dep in invalid for dep in self.plan[name]["dependencies"]):
                invalid.add(name)
        if reset_baseline or invalid.intersection(legacy):
            print("  Dependency baseline/group invalidated; rebuilding its consumers.", flush=True)
            invalid.update(self.plan)
            self.state.pop("baseline", None)
            self.state.pop("legacy", None)
            _safe_remove(self.prefix, self.workspace)
        for name in invalid:
            print(f"  Invalidating dependency {name} and its affected consumers.", flush=True)
            entries.pop(name, None)
            if name in self.plan:
                _preserve_build_diagnostics(Path(self.plan[name]["prefix"]), self.workspace)
            (self.build_dir / "CMakeFiles" / f"{name}-complete").unlink(missing_ok=True)
            if pending and pending["name"] == name:
                self.state.pop("pending", None)
        if "baseline" in self.state:
            self.restore()
        self.save()
        return bool(invalid)

    def configured(self) -> None:
        if "baseline" not in self.state:
            self.state["baseline"] = self.snapshot(
                _dependency_files(self.prefix),
                hashlib.sha256((str(self.prefix) + "-baseline").encode()).hexdigest())
        self.restore()
        self.save()

    def build(self, env: dict[str, str], jobs: int) -> None:
        entries = self.state["dependencies"]
        for name in self.order:
            node = self.plan[name]
            if name in entries:
                print(f"  Dependency {name}: verified cache - skipping build", flush=True)
                continue
            before = _dependency_files(self.prefix)
            print(f"  Dependency {name}: building/resuming", flush=True)
            self.state["pending"] = {"name": name, "key": node["key"], "work_digest": ""}
            self.save()
            try:
                _run([shutil.which("cmake") or "cmake", "--build", str(self.build_dir),
                      "--target", name, "--parallel", str(jobs)], env=env, stream=True)
            except BaseException:
                self.state["pending"]["work_digest"] = self.work_digest(Path(node["prefix"]))
                self.save()
                raise
            after = _dependency_files(self.prefix)
            if set(before) - set(after):
                raise AddonBuildError(f"dependency {name} removed another dependency's installed outputs")
            files = {relative: metadata for relative, metadata in after.items() if before.get(relative) != metadata}
            entries[name] = {
                "key": node["key"], "snapshot": self.snapshot(files, node["key"]),
                "work_digest": self.work_digest(Path(node["prefix"])),
            }
            self.state.pop("pending", None)
            self.save()


def _build_target(
    root: Path,
    workspace: Path,
    target_name: str,
    target: dict[str, Any],
    addon_ids: list[str],
    source: dict[str, str],
    source_root: Path,
    sdk: Path,
    depends: Path,
    jobs: int,
    fingerprint: str,
    hasher: _InputHasher,
    record: dict[str, Any],
    runtime_sdk: Path,
    archive_map: Path,
) -> list[Path]:
    entries: dict[str, Any] = record.setdefault("targets", {})
    entries.pop(target_name, None)
    _refresh_prefixes(record)
    record_path = workspace / "build-record.json"
    if record_path.exists():
        _write_if_changed(record_path, _json_bytes(record))

    dependency_context = _dependency_context(
        hasher, root, target_name, addon_ids, sdk, depends, workspace, runtime_sdk, archive_map)
    dependencies = _DependencyBuild(workspace, target_name, fingerprint, dependency_context, hasher)
    build_dir = dependencies.build_dir
    install_prefix = dependencies.install_prefix
    source_prefix = build_dir / "source-prefix"
    definitions = build_dir / "addon-definitions"
    _write_if_changed(
        definitions / target_name / f"{target_name}.txt",
        f"{target_name} {source['url']} {source['sha']}\n".encode("utf-8"),
    )
    _write_if_changed(definitions / target_name / "platforms.txt", b"all\n")
    if dependencies.changed:
        _stage_sources(source_root, source_prefix, [target_name], target_name, [target_name], workspace)
    if dependencies.changed or dependencies.state.get("finished"):
        _preserve_build_diagnostics(build_dir / f"{target_name}-prefix", workspace)
        _safe_remove(install_prefix, workspace)
        (build_dir / "CMakeFiles" / f"{target_name}-complete").unlink(missing_ok=True)
    dependencies.state.pop("finished", None)

    local_depends = build_dir / "build" / "depends"
    toolchain = build_dir / "toolchain" / "Toolchain_binaddons.cmake"
    roots = [local_depends, depends, sdk / "target" / "user" / "homebrew"]
    if (sdk / "sysroot" / "usr").is_dir():
        roots.append(sdk / "sysroot" / "usr")
    settings = {
        "PS5_PAYLOAD_SDK": sdk,
        "PS5_NATIVE_RUNTIME_SDK": runtime_sdk,
        "PS5_NATIVE_DEPENDS_MAP": archive_map,
        "PS5_DEPENDS_PREFIX": depends,
    }
    rendered = "".join(f"set({name} {_cmake_quote(str(path))} CACHE PATH \"\" FORCE)\n"
                       for name, path in settings.items())
    rendered += ("include(" + _cmake_quote(str(root / "tools/ps5/toolchain/ps5-native-addon.cmake")) +
                 ")\nlist(PREPEND CMAKE_FIND_ROOT_PATH " +
                 " ".join(_cmake_quote(str(path)) for path in roots) + ")\n")
    _write_if_changed(toolchain, rendered.encode("utf-8"))
    env = os.environ.copy()
    env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)
    env["PS5_DEPENDS_PREFIX"] = str(depends)
    env["PS5_NATIVE_RUNTIME_SDK"] = str(runtime_sdk)
    env["PS5_NATIVE_DEPENDS_MAP"] = str(archive_map)
    config_site = build_dir / "toolchain" / "config.site"
    _write_if_changed(config_site, _autoconf_site(sdk, runtime_sdk).encode("utf-8"))
    env["CONFIG_SITE"] = str(config_site)
    command = [
        shutil.which("cmake") or "cmake",
        "-S",
        str(root / "cmake" / "addons"),
        "-B",
        str(build_dir),
        "-G",
        "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        f"-DCORE_SOURCE_DIR={root}",
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        f"-DBUILD_DIR={build_dir / 'build'}",
        f"-DADDON_DEPENDS_PATH={local_depends}",
        f"-DCMAKE_PREFIX_PATH={depends};{local_depends};{sdk / 'target' / 'user' / 'homebrew'}",
        f"-DADDON_SRC_PREFIX={source_prefix}",
        f"-DADDONS_DEFINITION_DIR={definitions}",
        f"-DADDONS_TO_BUILD={target_name}",
        "-DCORE_SYSTEM_NAME=freebsd",
        "-DCORE_PLATFORM_NAME=ps5",
        "-DCPU=x86_64",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DOVERRIDE_PATHS=ON",
        "-DCMAKE_INSTALL_LIBDIR=lib/kodi",
        "-DBUILD_TESTING=OFF",
        f"-DPS5_ADDON_DEPENDENCY_PLAN={dependencies.plan_path}",
    ]
    autoconf_files = _autoconf_files(root, depends)
    if autoconf_files:
        command.append("-DAUTOCONF_FILES=" + " ".join(str(path) for path in autoconf_files))
    sdk_pkgconfig_lib_dirs, sdk_pkgconfig_path_dirs = _prepare_sdk_pkgconfig(sdk, build_dir)
    pkgconfig_lib_dirs = [
        depends / "lib" / "pkgconfig",
        depends / "share" / "pkgconfig",
        local_depends / "lib" / "pkgconfig",
        local_depends / "share" / "pkgconfig",
        *sdk_pkgconfig_lib_dirs,
    ]
    pkgconfig_path_dirs = [
        *sdk_pkgconfig_path_dirs,
        local_depends / "libdata" / "pkgconfig",
    ]
    pkg_config = _pkg_config_tool(root, depends)
    pkg_config_wrapper = build_dir / "host-tools" / "pkg-config"
    _write_if_changed(
        pkg_config_wrapper,
        ("#!/bin/sh\nexec " + shlex.quote(pkg_config) + ' "$@"\n').encode("utf-8"),
    )
    pkg_config_wrapper.chmod(0o755)
    host_paths = [str(pkg_config_wrapper.parent)]
    native_root = _native_root(root, depends)
    if native_root:
        host_paths.append(str(native_root / "bin"))
    env["PATH"] = os.pathsep.join([*host_paths, env.get("PATH", os.defpath)])
    env["PKG_CONFIG"] = str(pkg_config_wrapper)
    env["PKG_CONFIG_LIBDIR"] = os.pathsep.join(str(path) for path in pkgconfig_lib_dirs)
    env["PKG_CONFIG_PATH"] = os.pathsep.join(str(path) for path in pkgconfig_path_dirs)
    env.pop("PKG_CONFIG_SYSROOT_DIR", None)
    env.pop("PKG_CONFIG_DIR", None)
    dependencies.environment = {
        name: env.get(name) for name in (
            *dependency_context["environment"], "PKG_CONFIG", "PKG_CONFIG_PATH",
            "PKG_CONFIG_LIBDIR", "CONFIG_SITE", "PS5_DEPENDS_PREFIX",
            "PS5_NATIVE_RUNTIME_SDK", "PS5_NATIVE_DEPENDS_MAP",
        )
    }
    try:
        print(f"  Configuring {target_name}", flush=True)
        _write_if_changed(dependencies.plan_path, b"")
        _run(command, env=env, stream=True)
        if dependencies.prepare():
            _run(command, env=env, stream=True)
        dependencies.configured()
        dependencies.build(env, jobs)
        print(f"  Compiling/installing {target_name} ({jobs} jobs)", flush=True)
        _run(
            [shutil.which("cmake") or "cmake", "--build", str(build_dir), "--target", target_name, "--parallel", str(jobs)],
            env=env,
            stream=True,
        )
        installed = _expected_installs(install_prefix, addon_ids)
        for directory in installed:
            native_installed(directory, install_prefix, runtime_sdk)
        if (_target_fingerprint(hasher, root, target_name, addon_ids, source, sdk,
                                depends, workspace, source_root, runtime_sdk, archive_map) != fingerprint
                or _dependency_context(hasher, root, target_name, addon_ids, sdk,
                                       depends, workspace, runtime_sdk, archive_map) != dependency_context):
            raise AddonBuildError("add-on build inputs changed during compilation; refusing publication")
        staged_source_digest = hasher.tree_digest([source_prefix])
    except BaseException:
        dependencies.save()
        entries.pop(target_name, None)
        _refresh_prefixes(record)
        if record_path.exists():
            _write_if_changed(record_path, _json_bytes(record))
        raise

    relative_installs = [str(path.relative_to(install_prefix)) for path in installed]
    artifacts: dict[str, str] = {}
    for artifact in _install_artifacts(install_prefix, installed):
        relative = str(artifact.relative_to(install_prefix))
        artifacts[relative] = hasher.file_digest(artifact)
    entries[target_name] = {
        "target": target_name,
        "url": source["url"],
        "ref": source["ref"],
        "source_revision": source["sha"],
        "fingerprint": fingerprint,
        "build_dir": str(build_dir),
        "staged_source_digest": staged_source_digest,
        "addons": addon_ids,
        "prefix": str(install_prefix.resolve()),
        "installed": relative_installs,
        "artifacts": artifacts,
    }
    _refresh_prefixes(record)
    _write_if_changed(record_path, _json_bytes(record))
    dependencies.state["finished"] = True
    dependencies.save()
    return installed


def _record_build_dir(workspace: Path, target_name: str, fingerprint: str, entry: Any) -> Path:
    if isinstance(entry, dict) and isinstance(entry.get("build_dir"), str):
        directory = Path(entry["build_dir"])
        if not directory.is_absolute() or not _inside(directory.resolve(), workspace.resolve()):
            raise AddonBuildError(f"build record directory escapes workspace: {directory}")
        return directory
    return workspace / "build" / target_name / fingerprint


def build(
    root: Path,
    catalogue: dict[str, Any],
    workspace: Path,
    only: list[str],
    jobs: int,
    sdk: Path,
    depends: Path,
    refresh: bool = False,
    runtime_sdk: Path | None = None,
    archive_map: Path | None = None,
) -> list[Path]:
    """Build selected add-ons and return one absolute install prefix per target."""
    root = Path(root).resolve()
    workspace = Path(workspace).resolve()
    sdk = Path(sdk).resolve()
    depends = Path(depends).resolve()
    runtime_sdk = Path(runtime_sdk or os.environ.get(
        "PS5_NATIVE_RUNTIME_SDK", root / "build/ps5-native/sdk")).resolve()
    archive_map = Path(archive_map or os.environ.get(
        "PS5_NATIVE_DEPENDS_MAP", root / "build/ps5-native/depends/archive-map.json")).resolve()
    validate_sdk(runtime_sdk, archive_map)
    if jobs < 1:
        raise AddonBuildError("jobs must be at least 1")
    targets, addons = _validate_catalogue(catalogue)
    selected = _selected_addons(addons, only)
    _validate_environment(root, sdk, depends)
    workspace.mkdir(parents=True, exist_ok=True)
    resolved = _resolve_sources(
        workspace,
        {name: targets[name] for name in {addons[addon_id]["target"] for addon_id in selected}},
        refresh,
    )
    record_path = workspace / "build-record.json"
    record = _read_json(record_path, {"schema": RECORD_VERSION, "prefixes": [], "targets": {}})
    if record.get("schema") == 2:
        print("Discarding legacy SO build cache records; retaining locked source downloads.", flush=True)
        record = {"schema": RECORD_VERSION, "prefixes": [], "targets": {}}
    if (
        record.get("schema") != RECORD_VERSION
        or not isinstance(record.get("prefixes"), list)
        or not isinstance(record.get("targets"), dict)
    ):
        raise AddonBuildError(f"unsupported build record: {record_path}")
    hasher = _InputHasher(workspace)

    grouped: dict[str, list[str]] = {}
    for addon_id in selected:
        grouped.setdefault(addons[addon_id]["target"], []).append(addon_id)

    for index, target_name in enumerate(sorted(grouped), 1):
        progress = f"[{index}/{len(grouped)}] {target_name}"
        print(f"{progress}: checking source and cache", flush=True)
        addon_ids = sorted(grouped[target_name])
        source = resolved[target_name]
        source_cache_path = workspace / "sources" / target_name / source["sha"]
        source_was_cached = _source_cache_valid(
            source_cache_path, {"url": source["url"], "sha": source["sha"]}
        )
        if not source_was_cached:
            removed = False
            entry = record["targets"].get(target_name)
            if isinstance(entry, dict) and entry.get("source_revision") == source["sha"]:
                del record["targets"][target_name]
                removed = True
            if removed and record_path.exists():
                _refresh_prefixes(record)
                _write_if_changed(record_path, _json_bytes(record))
        source_root = _fetch_source(workspace, target_name, source["url"], source["sha"])
        if not _source_cache_valid(source_root, source):
            raise AddonBuildError(f"invalid cached source for {target_name}: {source_root}")
        fingerprint = _target_fingerprint(
            hasher, root, target_name, addon_ids, source, sdk, depends, workspace, source_root,
            runtime_sdk, archive_map,
        )
        build_source_prefix = _record_build_dir(
            workspace, target_name, fingerprint, record["targets"].get(target_name)) / "source-prefix"
        staged_source_digest = (
            hasher.tree_digest([build_source_prefix]) if build_source_prefix.is_dir() else None
        )
        cached = _record_entry_valid(
            record["targets"].get(target_name),
            target_name,
            addon_ids,
            fingerprint,
            source,
            workspace,
            staged_source_digest,
            hasher,
        )
        if (
            source_was_cached
            and cached is not None
            and (build_source_prefix / target_name / "CMakeLists.txt").is_file()
        ):
            print(f"{progress}: cached - skipping build", flush=True)
            continue
        print(f"{progress}: building", flush=True)
        _build_target(
                root,
                workspace,
                target_name,
                targets[target_name],
                addon_ids,
                source,
                source_root,
                sdk,
                depends,
                jobs,
                fingerprint,
                hasher,
                record,
                runtime_sdk,
                archive_map,
        )
        print(f"{progress}: complete", flush=True)
    hasher.save()
    return [
        Path(record["targets"][target_name]["prefix"])
        for target_name in sorted(grouped)
    ]


def validate_build(
    root: Path,
    catalogue: dict[str, Any],
    workspace: Path,
    sdk: Path,
    depends: Path,
    runtime_sdk: Path | None = None,
    archive_map: Path | None = None,
) -> dict[str, Any]:
    """Validate every catalogue target from the source lock without network access."""
    root = Path(root).resolve()
    workspace = Path(workspace).resolve()
    sdk = Path(sdk).resolve()
    depends = Path(depends).resolve()
    runtime_sdk = Path(runtime_sdk or os.environ.get(
        "PS5_NATIVE_RUNTIME_SDK", root / "build/ps5-native/sdk")).resolve()
    archive_map = Path(archive_map or os.environ.get(
        "PS5_NATIVE_DEPENDS_MAP", root / "build/ps5-native/depends/archive-map.json")).resolve()
    validate_sdk(runtime_sdk, archive_map)
    targets, addons = _validate_catalogue(catalogue)
    _validate_environment(root, sdk, depends)
    record_path = workspace / "build-record.json"
    record = _read_json(record_path, {"schema": RECORD_VERSION, "prefixes": [], "targets": {}})
    if (
        record.get("schema") != RECORD_VERSION
        or not isinstance(record.get("prefixes"), list)
        or not isinstance(record.get("targets"), dict)
    ):
        raise AddonBuildError(f"unsupported build record: {record_path}")

    target_addons: dict[str, list[str]] = {}
    for addon_id, addon in addons.items():
        target_addons.setdefault(addon["target"], []).append(addon_id)
    pinned_sources = _locked_sources(workspace, {name: targets[name] for name in target_addons})
    hasher = _InputHasher(workspace)
    expected_prefixes: list[str] = []

    for target_name in sorted(target_addons):
        addon_ids = sorted(target_addons[target_name])
        source = pinned_sources[target_name]
        source_root = workspace / "sources" / target_name / source["sha"]
        if not _source_cache_valid(source_root, source):
            raise AddonBuildError(
                f"source cache for {target_name} is missing or invalid: {source_root}"
            )
        fingerprint = _target_fingerprint(
            hasher,
            root,
            target_name,
            addon_ids,
            source,
            sdk,
            depends,
            workspace,
            source_root,
            runtime_sdk,
            archive_map,
        )
        build_source_prefix = _record_build_dir(
            workspace, target_name, fingerprint, record["targets"].get(target_name)) / "source-prefix"
        staged_source_digest = (
            hasher.tree_digest([build_source_prefix])
            if build_source_prefix.is_dir()
            else None
        )
        installed = _record_entry_valid(
            record["targets"].get(target_name),
            target_name,
            addon_ids,
            fingerprint,
            source,
            workspace,
            staged_source_digest,
            hasher,
        )
        if installed is None:
            raise AddonBuildError(
                f"build record for {target_name} is missing, stale, or has missing/modified outputs"
            )
        for directory in installed:
            native_installed(directory, Path(record["targets"][target_name]["prefix"]), runtime_sdk)
        if not (build_source_prefix / target_name / "CMakeLists.txt").is_file():
            raise AddonBuildError(f"staged source for {target_name} is missing")
        prefix = record["targets"][target_name]["prefix"]
        expected_prefixes.append(str(Path(prefix).resolve()))

    expected_prefixes = sorted(set(expected_prefixes))
    if record["prefixes"] != expected_prefixes:
        raise AddonBuildError(
            f"build record prefix list is stale: expected {expected_prefixes}, got {record['prefixes']}"
        )
    hasher.save()
    return record


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Kodi source root")
    parser.add_argument("--catalogue", type=Path, required=True, help="PS5 add-on catalogue JSON")
    parser.add_argument("--workspace", type=Path, required=True, help="persistent builder workspace")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk")))
    parser.add_argument("--depends", type=Path, default=os.environ.get("PS5_DEPENDS_PREFIX"))
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--refresh-sources", action="store_true")
    args = parser.parse_args()
    if args.depends is None:
        parser.error("--depends or PS5_DEPENDS_PREFIX is required")
    catalogue = json.loads(args.catalogue.read_text(encoding="utf-8"))
    for path in build(
        args.root,
        catalogue,
        args.workspace,
        args.only,
        args.jobs,
        args.sdk,
        args.depends,
        args.refresh_sources,
    ):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
