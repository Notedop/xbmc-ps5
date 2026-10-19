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

from addon_elf import elf_problems


RUNTIME = "piers-ps5-1"
LOCK_VERSION = 1
RECORD_VERSION = 2
HASH_CACHE_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_PLACEHOLDER = re.compile(r"@([A-Za-z0-9_]+)@")


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


def _render_toolchain(template: Path, sdk: Path, find_roots: list[Path], destination: Path) -> str:
    original = template.read_text(encoding="utf-8")
    ccache = shutil.which("ccache") or ""
    variables = {
        "CMAKE_FIND_ROOT_PATH": ";".join(str(path.resolve()) for path in find_roots),
        "platform_os": "ps5",
        "host_cpu": "x86_64",
        "use_cpu": "x86_64",
        "target_platform": "ps5",
        "app_rendersystem": "gl",
        "target_minver": "",
        "use_sdk_path": "",
        "CC": str(sdk / "bin" / "prospero-clang"),
        "CXX": str(sdk / "bin" / "prospero-clang++"),
        "AR": str(sdk / "bin" / "prospero-ar"),
        "LD": str(sdk / "bin" / "prospero-lld"),
        "NM": str(sdk / "bin" / "prospero-nm"),
        "OBJDUMP": "",
        "RANLIB": str(sdk / "bin" / "prospero-ranlib"),
        "use_ccache": "yes" if ccache else "",
        "CCACHE": ccache,
        "use_toolchain": str(sdk),
        "use_host": "x86_64-unknown-freebsd",
        "use_ndk_path": "",
        "use_ndk_api": "",
        "build_tools_path": "",
        "platform_cppflags": "-fPIC -DPIC -D_LARGEFILE_SOURCE -D_LARGEFILE64_SOURCE -D_FILE_OFFSET_BITS=64",
        "platform_cflags": "-Og -g -D_DEBUG",
        "platform_cxxflags": "-Og -g -D_DEBUG",
        "platform_includes": "",
        "platform_cflags_release": "-DNDEBUG=1 -O2",
        "platform_cxxflags_release": "-DNDEBUG=1 -O2",
        "platform_cppflags_release": "-fPIC -DPIC -D_LARGEFILE_SOURCE -D_LARGEFILE64_SOURCE -D_FILE_OFFSET_BITS=64",
        "platform_includes_release": "",
        "platform_cflags_debug": "-Og -g -D_DEBUG",
        "platform_cxxflags_debug": "-Og -g -D_DEBUG",
        "platform_cppflags_debug": "-fPIC -DPIC -D_LARGEFILE_SOURCE -D_LARGEFILE64_SOURCE -D_FILE_OFFSET_BITS=64",
        "platform_includes_debug": "",
        "platform_ldflags": "",
    }

    def replace_quoted(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise AddonBuildError(f"unknown placeholder @{key}@ in {template}")
        return _cmake_quote(variables[key])

    rendered = re.sub(r'"@([A-Za-z0-9_]+)@"', replace_quoted, original)

    def replace_plain(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise AddonBuildError(f"unknown placeholder @{key}@ in {template}")
        preceding = match.string[:match.start()]
        quoted = False
        escaped = False
        for character in preceding:
            if character == '"' and not escaped:
                quoted = not quoted
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        value = _cmake_escape(variables[key])
        return value if quoted else f'"{value}"'

    rendered = _PLACEHOLDER.sub(replace_plain, rendered)
    rendered = rendered.replace("/opt/ps5-payload-sdk", str(sdk.resolve()).replace("\\", "/"))
    rendered = rendered.replace('"yes"', _cmake_quote("yes" if ccache else ""))
    rendered = rendered.replace("/usr/bin/ccache", ccache or "")
    if _PLACEHOLDER.search(rendered):
        raise AddonBuildError(f"unresolved placeholder in rendered toolchain from {template}")
    _write_if_changed(destination, rendered.encode("utf-8"))
    return rendered


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
    if not (root / "tools" / "depends" / "target" / "Toolchain_binaddons.cmake.in").is_file():
        raise AddonBuildError("missing tools/depends/target/Toolchain_binaddons.cmake.in")
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
    root: Path, addon_ids: list[str], sdk: Path, depends: Path
) -> list[Path]:
    ccache = shutil.which("ccache")
    pkg_config = _pkg_config_tool(root, depends)
    inputs = [
        root / "tools" / "ps5" / "addon_build.py",
        root / "tools" / "ps5" / "addon_elf.py",
        root / "cmake" / "addons" / "CMakeLists.txt",
        root / "cmake" / "KodiConfig.cmake.in",
        root / "cmake" / "addons" / "depends",
        root / "cmake" / "scripts" / "common",
        root / "cmake" / "modules",
        root / "cmake" / "platform" / "freebsd",
        root / "cmake" / "platform" / "ps5",
        root / "tools" / "depends" / "target" / "Toolchain_binaddons.cmake.in",
        root / "tools" / "depends" / "target" / "config-binaddons.site",
        root / "tools" / "depends" / "xbmc-addons.include",
        root / "tools" / "depends" / "Makefile.include",
        root / "xbmc" / "addons" / "kodi-dev-kit" / "include",
        *_autoconf_files(root, depends),
        root / "tools" / "ps5" / "apply-adaptive-patch.cmake",
        root / "tools" / "ps5" / "apply-patch-series.cmake",
        root / "tools" / "ps5" / "apply-source-patch.cmake",
        root / "tools" / "ps5" / "inputstream-adaptive-ps5.patch",
        depends,
        *_sdk_inputs(sdk),
        Path(ccache) if ccache else None,
        Path(pkg_config) if Path(pkg_config).is_absolute() else None,
        shutil.which("cmake") and Path(shutil.which("cmake") or ""),
        shutil.which("ninja") and Path(shutil.which("ninja") or ""),
    ]
    for addon_id in addon_ids:
        addon_definition = root / "cmake" / "addons" / "addons" / addon_id
        if addon_definition.exists():
            inputs.append(addon_definition)
        patch_dir = root / "tools" / "ps5" / "patches" / "addons" / addon_id
        if patch_dir.exists():
            inputs.append(patch_dir)
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
    metadata = directory / "addon.xml"
    if not metadata.is_file():
        raise AddonBuildError(f"installed add-on metadata is missing: {directory / 'addon.xml'}")
    library = ET.parse(metadata).getroot().find("./extension[@library_freebsd]")
    name = library.get("library_freebsd", "") if library is not None else ""
    if not name or Path(name).name != name or "\\" in name:
        raise AddonBuildError(f"invalid/missing library_freebsd in {metadata}")
    binary = prefix / "lib/kodi/addons" / directory.name / name
    if not binary.is_file():
        raise AddonBuildError(f"installed add-on binary is missing: {binary}")
    _install_artifacts(prefix, [directory])
    companions = sorted(path for path in binary.parent.glob("lib*.so")
                        if not path.is_symlink() and path != binary)
    for elf in [binary, *companions]:
        problems = elf_problems(elf)
        if problems:
            raise AddonBuildError(f"{elf} is not loadable on PS5: {'; '.join(problems)}")


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
        _cmake_inputs(root, addon_ids, sdk, depends),
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
) -> list[Path]:
    entries: dict[str, Any] = record.setdefault("targets", {})
    entries.pop(target_name, None)
    _refresh_prefixes(record)
    record_path = workspace / "build-record.json"
    if record_path.exists():
        _write_if_changed(record_path, _json_bytes(record))

    build_dir = workspace / "build" / target_name / fingerprint
    install_prefix = workspace / "install" / target_name / fingerprint
    if build_dir.exists():
        _safe_remove(build_dir, workspace)
    if install_prefix.exists():
        _safe_remove(install_prefix, workspace)
    source_prefix = build_dir / "source-prefix"
    _stage_sources(
        source_root,
        source_prefix,
        [target_name],
        target_name,
        [target_name],
        workspace,
    )

    local_depends = build_dir / "build" / "depends"
    toolchain = build_dir / "toolchain" / "Toolchain_binaddons.cmake"
    roots = [local_depends, depends, sdk / "target" / "user" / "homebrew"]
    if (sdk / "sysroot" / "usr").is_dir():
        roots.append(sdk / "sysroot" / "usr")
    _render_toolchain(
        root / "tools" / "depends" / "target" / "Toolchain_binaddons.cmake.in",
        sdk,
        roots,
        toolchain,
    )
    env = os.environ.copy()
    env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)
    env["PS5_DEPENDS_PREFIX"] = str(depends)
    config_site = build_dir / "toolchain" / "config.site"
    site_lines = [
        "cross_compiling=yes",
        "host=x86_64-unknown-freebsd",
        "host_alias=x86_64-unknown-freebsd",
    ]
    for variable, tool in (
        ("CC", "clang"), ("CXX", "clang++"), ("CPP", "clang"),
        ("AR", "ar"), ("LD", "lld"), ("NM", "nm"), ("RANLIB", "ranlib"),
        ("STRIP", "strip"),
    ):
        value = shlex.quote(str(sdk / "bin" / f"prospero-{tool}"))
        if variable == "CPP":
            value += " -E"
        site_lines.append(f"{variable}={shlex.quote(value)}")
    _write_if_changed(config_site, ("\n".join(site_lines) + "\n").encode("utf-8"))
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
        f"-DADDONS_TO_BUILD={target_name}",
        "-DCORE_SYSTEM_NAME=freebsd",
        "-DCORE_PLATFORM_NAME=ps5",
        "-DCPU=x86_64",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DOVERRIDE_PATHS=ON",
        "-DCMAKE_INSTALL_LIBDIR=lib/kodi",
        "-DBUILD_TESTING=OFF",
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
    try:
        print(f"  Configuring {target_name}", flush=True)
        _run(command, env=env, stream=True)
        print(f"  Compiling/installing {target_name} ({jobs} jobs)", flush=True)
        _run(
            [shutil.which("cmake") or "cmake", "--build", str(build_dir), "--target", target_name, "--parallel", str(jobs)],
            env=env,
            stream=True,
        )
        installed = _expected_installs(install_prefix, addon_ids)
        staged_source_digest = hasher.tree_digest([source_prefix])
    except BaseException:
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
        "staged_source_digest": staged_source_digest,
        "addons": addon_ids,
        "prefix": str(install_prefix.resolve()),
        "installed": relative_installs,
        "artifacts": artifacts,
    }
    _refresh_prefixes(record)
    _write_if_changed(record_path, _json_bytes(record))
    return installed


def build(
    root: Path,
    catalogue: dict[str, Any],
    workspace: Path,
    only: list[str],
    jobs: int,
    sdk: Path,
    depends: Path,
    refresh: bool = False,
) -> list[Path]:
    """Build selected add-ons and return one absolute install prefix per target."""
    root = Path(root).resolve()
    workspace = Path(workspace).resolve()
    sdk = Path(sdk).resolve()
    depends = Path(depends).resolve()
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
            hasher, root, target_name, addon_ids, source, sdk, depends, workspace, source_root
        )
        build_source_prefix = workspace / "build" / target_name / fingerprint / "source-prefix"
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
) -> dict[str, Any]:
    """Validate every catalogue target from the source lock without network access."""
    root = Path(root).resolve()
    workspace = Path(workspace).resolve()
    sdk = Path(sdk).resolve()
    depends = Path(depends).resolve()
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
        )
        build_source_prefix = (
            workspace / "build" / target_name / fingerprint / "source-prefix"
        )
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
