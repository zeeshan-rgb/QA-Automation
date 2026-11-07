"""Generate src/pavs/polarion/capabilities/data/endpoint_catalog.json.

Inputs: the newest Polarion REST OpenAPI spec and the SDK REST change log
(both ship in every Polarion install under polarion/sdk/doc/rest/).

    python tools/build_endpoint_catalog.py E:/polarion/sdk/doc/rest

Each operation gets `since` (first release that lists it as new; null = older than the
oldest release in the change log), `removed_in`, and `modified_in`.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "src" / "pavs" / "polarion" / "capabilities" / "data" / "endpoint_catalog.json"
METHODS = ("get", "post", "patch", "delete", "put")


def release_code(internal: str) -> str:
    m = re.match(r"3\.(\d{2})\.(\d{1,2})$", internal)
    if not m:
        raise ValueError(internal)
    return f"{m.group(1)}{int(m.group(2)):02d}"


def norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def parse_changes(text: str):
    """-> (releases newest first, since{}, removed{}, modified{})"""
    releases: list[str] = []
    since: dict[tuple[str, str], str] = {}
    removed: dict[tuple[str, str], str] = {}
    modified: dict[tuple[str, str], list[str]] = {}
    version = None
    section = None
    for line in text.splitlines():
        m = re.match(r"^Version (\S+)", line)
        if m:
            version = release_code(m.group(1))
            releases.append(version)
            section = None
            continue
        if "### New Endpoints" in line:
            section = "new"
            continue
        if "### Deleted Endpoints" in line:
            section = "deleted"
            continue
        if "### Modified Endpoints" in line:
            section = "modified"
            continue
        m = re.match(r"^\s{2}(GET|POST|PATCH|DELETE|PUT) (/\S*)\s*$", line)
        if m and version and section:
            key = (m.group(1), norm(m.group(2)))
            if section == "new":
                since[key] = version          # newest-first file: older entries overwrite
            elif section == "deleted":
                removed.setdefault(key, version)
            elif section == "modified":
                modified.setdefault(key, []).append(version)
    return releases, since, removed, modified


def main(rest_dir: str) -> None:
    rest = Path(rest_dir)
    spec = json.loads((rest / "polarionrest.json").read_text(encoding="utf-8"))
    releases, since, removed, modified = parse_changes((rest / "changes.txt").read_text(encoding="utf-8"))
    ops = []
    for path, item in spec["paths"].items():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            key = (method.upper(), norm(path))
            ops.append({
                "method": method.upper(), "path": path,
                "tag": (op.get("tags") or [""])[0], 
                "since": since.get(key), "removed_in": removed.get(key),
                "modified_in": sorted(set(modified.get(key, []))),
            })
    # endpoints that were deleted and are therefore not in the newest spec
    for key, rel in removed.items():
        if not any((o["method"], norm(o["path"])) == key for o in ops):
            ops.append({"method": key[0], "path": key[1], "tag": "", "summary": "(removed)",
                        "since": since.get(key), "removed_in": rel, "modified_in": []})
    catalog = {
        "generated": date.today().isoformat(),
        "source_spec_version": releases[0] if releases else None,
        "oldest_logged_release": releases[-1] if releases else None,
        "releases": sorted(releases),
        "note": "since=null means the endpoint predates oldest_logged_release.",
        "operations": sorted(ops, key=lambda o: (o["path"], o["method"])),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(catalog, indent=1), encoding="utf-8")
    print(f"{len(ops)} operations, releases {catalog['releases']} -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "E:/polarion/sdk/doc/rest")
