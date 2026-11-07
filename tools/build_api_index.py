"""Generate src/pavs/engines/analysis/data/api_methods.json from a Polarion install's SDK.

    python tools/build_api_index.py E:/polarion/sdk

Inputs: sdk/changes.txt (added / removed / deprecated per release) and the Javadoc member index.
Output maps a method's SIMPLE name to:
  since   - the first release in the change log that ADDED a method with that name (earliest over all classes)
  removed - the release that removed it (only if no class still has it)
  known   - every method name present in the current API (to tell 'old' from 'not in the API at all')
Names are ambiguous across classes, so the analyzer only reports a name when EVERY class carrying it is newer.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "src" / "pavs" / "engines" / "analysis" / "data" / "api_methods.json"


def code(version: str) -> str:
    m = re.match(r"3\.(\d{2})\.(\d{1,2})$", version)
    if not m or int(m.group(1)) < 20:
        return "0000"            # before the YYMM naming: treated as ancient
    return f"{m.group(1)}{int(m.group(2)):02d}"


def main(sdk: str) -> None:
    sdk_dir = Path(sdk)
    text = (sdk_dir / "changes.txt").read_text(encoding="utf-8", errors="replace")
    version = None
    added: dict[str, dict[str, str]] = {}          # name -> {class fqcn -> release it was added}
    removed: dict[str, set[str]] = {}
    oldest = None
    for line in text.splitlines():
        m = re.match(r"^Version (3\.\d+\.\d+)\s*$", line)
        if m:
            version = code(m.group(1))
            oldest = version
            continue
        m = re.match(r"^\s*\*\s+(added|removed) method ([\w.$]+)\.(\w+)\(", line)
        if m and version:
            cls, name = m.group(2).replace("$", "."), m.group(3)
            if m.group(1) == "added":
                added.setdefault(name, {})
                prev = added[name].get(cls)
                added[name][cls] = version if prev is None else min(prev, version)
            else:
                removed.setdefault(name, set()).add(version)

    carriers: dict[str, set[str]] = {}              # name -> classes that have it in the CURRENT API
    idx = sdk_dir / "doc" / "javadoc" / "member-search-index.js"
    if idx.exists():
        for pkg, cls, lab in re.findall(r'"p":"([^"]+)","c":"([^"]+)","l":"([A-Za-z_]\w*)(?:\([^"]*\))?"',
                                        idx.read_text(encoding="utf-8", errors="replace")):
            carriers.setdefault(lab, set()).add(f"{pkg}.{cls}")
    known = sorted(carriers)

    # a name is NEW only when every class that carries it was added in the log (otherwise an older class has it)
    since = {}
    for name, per_class in added.items():
        have = carriers.get(name, set())
        if have and have <= set(per_class):
            since[name] = min(per_class[c] for c in have)
    gone = {n: max(v) for n, v in removed.items() if n not in carriers}
    data = {"oldest_logged_release": oldest, "since": since, "removed": gone, "known": known}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"{len(since)} names that are new in every class, {len(gone)} removed names, {len(known)} known names -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "E:/polarion/sdk")
