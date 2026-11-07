"""Render docs/COMPATIBILITY.md from the capability registry + recorded real-server checks.

    python tools/render_compatibility.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pavs.polarion.capabilities import CapabilityRegistry  # noqa: E402
from pavs.services.compatibility import CompatibilityService  # noqa: E402

# Facts checked against REAL servers. Add a row only with evidence (date, server, how).
REAL_SERVER_CHECKS = [
    ("2606", "2026-11-06", "local install (localhost)",
     "Login page buildId + 'Version 2606', SDK changes.txt 3.26.6, /rest/v1/definition (284 ops) "
     "without auth; REST 401 JSON without token; SOAP WSDLs served; SOAP envelope + sessionID header "
     "+ 'Authentication failed' fault shape; /repo 401 Basic realm 'Subversion repository'. "
     "Authenticated capabilities NOT yet verified (needs a PAT)."),
    ("2606", "2026-11-06", "local install, repository read via svnlook (read-only)",
     "Phase 2 SVN configuration readers on project drivepilot: roles, user-roles, permissions.xml (both grant "
     "styles + customsets), projects.xml path mapping, enums (incl. type-specific), custom-fields, workflows "
     "(Work Item, Document, Test Run; conditions/functions/signatures), read-only fields and form filters. "
     "SOAP WSDL types used by the readers (EnumOption, CustomFieldType, WorkflowAction, Module, Project) read "
     "from the live WSDL."),
]


def main() -> None:
    svc = CompatibilityService(CapabilityRegistry.load())
    scenarios, rows = svc.documented_matrix()
    out = ["# Compatibility matrix", "",
           f"Generated {date.today().isoformat()} by `tools/render_compatibility.py`. Do not edit by hand.", "",
           "## How to read this", "",
           "- **Documented** cells come from the Polarion SDK REST spec + change log and the SOAP API, "
           "assuming REST and SOAP are on and signed in. They are *not* proof that the feature works on that release.",
           "- A release is only called **supported** once the compatibility suite has run against a real server of "
           "that release and recorded VERIFIED results (table at the bottom).",
           "- 'SVN reader (opt-in)' = only the read-only SVN configuration reader can provide it; it must be enabled per environment.",
           "- Releases 2503 and 2603 are not listed: they do not exist in Siemens' SDK change log "
           "(real releases: 2410, 2506, 2512, 2606). An unknown release uses the nearest lower adapter + live probing.", "",
           "## Documented capability matrix", ""]
    out.append("| Area | Capability | " + " | ".join(s.label for s in scenarios) + " |")
    out.append("|---|---|" + "---|" * len(scenarios))
    for r in rows:
        cells = []
        for sc in scenarios:
            status, prov = r.cells.get(sc.key, (None, ""))
            if prov == "svn opt-in":
                cells.append("SVN (opt-in)")
            elif status is None:
                cells.append("-")
            else:
                cells.append(f"{status.value.replace('_', ' ')}{' (' + prov.upper() + ')' if prov else ''}")
        out.append(f"| {r.area} | {r.title} | " + " | ".join(cells) + " |")
    out += ["", "## Adapters and known quirks", ""]
    for a in svc.adapters_info():
        out.append(f"- **{a['name']}** - {'validated' if a['validated'] else 'not yet validated'}")
        for q in a["quirks"]:
            out.append(f"  - {q}")
    out += ["", "## Checks against real servers", "",
            "| Release | Date | Server | What was verified |", "|---|---|---|---|"]
    for rel, when, server, what in REAL_SERVER_CHECKS:
        out.append(f"| {rel} | {when} | {server} | {what} |")
    out += ["", "Not yet checked on a real server: 2410 (REST off), 2506, 2512; any authenticated "
            "capability on 2606.", ""]
    path = ROOT / "docs" / "COMPATIBILITY.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {path} ({len(rows)} capabilities x {len(scenarios)} scenarios)")


if __name__ == "__main__":
    main()
