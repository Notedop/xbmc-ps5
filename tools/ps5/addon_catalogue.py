"""Shared selection and manifest policy for PS5 title and repository packages."""

import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

DEFAULT_CATALOGUE = Path(__file__).with_name("addon-catalogue.json")
RUNTIME_ADDON = "resource.ps5.addon-runtime"
RUNTIME_VERSION = "1.0.0"
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_@~]*")
SHA256 = re.compile(r"[0-9a-f]{64}")


def load_catalogue(path=DEFAULT_CATALOGUE):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or
            not isinstance(data.get("runtime"), str) or
            not IDENTIFIER.fullmatch(data["runtime"])):
        raise ValueError("invalid PS5 catalogue schema/runtime")
    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets or "addons" in data:
        raise ValueError("catalogue must contain targets with nested add-on policies")
    addons = {}
    for target, source in targets.items():
        if not IDENTIFIER.fullmatch(target) or not isinstance(source, dict):
            raise ValueError(f"invalid build target: {target}")
        if (not isinstance(source.get("url"), str) or
                not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                                 source["url"])):
            raise ValueError(f"invalid source URL for {target}")
        ref = source.get("ref", "")
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(
                c.isspace() for c in ref):
            raise ValueError(f"invalid source ref for {target}")
        policies = source.get("addons")
        if not isinstance(policies, dict) or not policies:
            raise ValueError(f"missing output IDs for {target}")
        for addon, policy in policies.items():
            if not isinstance(addon, str) or not IDENTIFIER.fullmatch(addon) or addon in addons:
                raise ValueError(f"invalid/duplicate output ID: {addon}")
            if not isinstance(policy, dict) or "target" in policy:
                raise ValueError(f"invalid policy for {addon}")
            addons[addon] = dict(policy, target=target)
        source["addons"] = list(policies)
    data["addons"] = addons
    for addon, policy in addons.items():
        if type(policy.get("bundled")) is not bool or type(policy.get("revision")) is not int:
            raise ValueError(f"invalid bundled/revision policy for {addon}")
        if policy["revision"] < 1 or policy.get("channel") not in ("stable", "testing"):
            raise ValueError(f"invalid revision/channel for {addon}")
        if policy["channel"] == "stable" and (
                not isinstance(policy.get("accepted_sha256"), str) or
                not SHA256.fullmatch(policy["accepted_sha256"]) or
                not isinstance(policy.get("acceptance"), str) or not policy["acceptance"].strip()):
            raise ValueError(f"stable {addon} needs an accepted ZIP SHA256 and console evidence")
    exclusions = json.loads(DEFAULT_CATALOGUE.with_name("addon-exclusions.json").read_text())
    blocked = set(addons) & set(exclusions)
    if blocked:
        raise ValueError(f"whitelist conflicts with exclusions: {', '.join(sorted(blocked))}")
    return data


def prepare_manifest(root, catalogue, addon_id):
    """Apply identical version, platform and runtime requirements in both outputs."""
    policy = catalogue["addons"][addon_id]
    if root.tag != "addon" or root.get("id") != addon_id:
        raise ValueError(f"manifest does not match whitelisted ID {addon_id}")
    version = root.get("version", "")
    if not VERSION.fullmatch(version):
        raise ValueError(f"invalid upstream version for {addon_id}: {version}")
    suffix = f"+ps5.{policy['revision']}"
    if "+ps5." in version:
        if not version.endswith(suffix):
            raise ValueError(f"conflicting PS5 package revision for {addon_id}: {version}")
    else:
        root.set("version", version + suffix)
    metadata = root.find("./extension[@point='xbmc.addon.metadata']")
    if metadata is None:
        metadata = ET.SubElement(root, "extension", {"point": "xbmc.addon.metadata"})
    platform = metadata.find("platform")
    if platform is None:
        platform = ET.SubElement(metadata, "platform")
    platform.text = "ps5"
    requires = root.find("requires")
    if requires is None:
        requires = ET.Element("requires")
        root.insert(0, requires)
    runtime = requires.find(f"./import[@addon='{RUNTIME_ADDON}']")
    if runtime is None:
        runtime = ET.SubElement(requires, "import", {"addon": RUNTIME_ADDON})
    runtime.set("version", RUNTIME_VERSION)
    runtime.attrib.pop("optional", None)
    return root
