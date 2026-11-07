# Compatibility matrix

Generated 2026-11-06 by `tools/render_compatibility.py`. Do not edit by hand.

## How to read this

- **Documented** cells come from the Polarion SDK REST spec + change log and the SOAP API, assuming REST and SOAP are on and signed in. They are *not* proof that the feature works on that release.
- A release is only called **supported** once the compatibility suite has run against a real server of that release and recorded VERIFIED results (table at the bottom).
- 'SVN reader (opt-in)' = only the read-only SVN configuration reader can provide it; it must be enabled per environment.
- Releases 2503 and 2603 are not listed: they do not exist in Siemens' SDK change log (real releases: 2410, 2506, 2512, 2606). An unknown release uses the nearest lower adapter + live probing.

## Documented capability matrix

| Area | Capability | 2410 | 2410 (REST off) | 2506 | 2512 | 2606 |
|---|---|---|---|---|---|---|
| customization | Workflow scripts and Velocity form scripts | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) |
| discovery | List projects | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| discovery | Users of a project | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| discovery | Server metadata (version, build, REST limits) | unsupported | unsupported | unsupported | supported (REST) | supported (REST) |
| discovery | Document and page spaces of a project | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| discovery | Current user | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| discovery | List users | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| documents | Baselines | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| documents | List Documents | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (REST) | supported (REST) |
| documents | Read a Document and its fields | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| documents | Document structure (parts, contained Work Items) | supported (REST) | partial (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| documents | Object history (revisions) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| reports | List pages (LiveReports / Info pages) | partial (SOAP) | partial (SOAP) | partial (SOAP) | supported (REST) | supported (REST) |
| reports | Page / widget / Velocity source | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) |
| roles | Effective permission check for a user | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| roles | Permission configuration (permissions.xml) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) |
| roles | User and group role assignments | partial (SOAP) | partial (SOAP) | partial (SOAP) | partial (SOAP) | partial (SOAP) |
| roles | Global roles | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| roles | Project (context) roles | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (SOAP) |
| roles | User groups | partial (REST) | SVN (opt-in) | partial (REST) | partial (REST) | partial (REST) |
| traceability | Test Runs and records | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| traceability | Back-linked Work Items | supported (SOAP) | supported (SOAP) | supported (SOAP) | supported (REST) | supported (REST) |
| traceability | Work Item links and link roles | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| workflow | Workflow definitions (statuses, transitions, conditions, functions) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) |
| workflow | Execute a workflow action on a test Work Item | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| workflow | Workflow actions of a Work Item (available, required fields, roles, blocked reason) | supported (REST) | partial (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| workitems | Custom field definitions | partial (SOAP) | partial (SOAP) | partial (SOAP) | supported (REST) | supported (REST) |
| workitems | List all enumerations | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | supported (REST) | supported (REST) |
| workitems | Read an enumeration | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| workitems | Field metadata per resource type | unsupported | unsupported | unsupported | supported (REST) | supported (REST) |
| workitems | Read-only fields and form filters | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) | SVN (opt-in) |
| workitems | Query Work Items (Lucene) | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |
| workitems | Work Item types | supported (REST) | supported (SOAP) | supported (REST) | supported (REST) | supported (REST) |

## Adapters and known quirks

- **Polarion 2410** - not yet validated
  - Save Hooks cannot be scoped to a Work Item type before 2506; scripts must filter by type themselves.
  - REST custom-field configuration endpoints arrive in 2512; older releases use SOAP or SVN.
  - REST /metadata arrives in 2512; the version comes from unauthenticated signals.
  - REST Document and Page listing arrive in 2512; older releases list Documents via SOAP queryModules.
  - REST back-links endpoint arrives in 2512; older releases use SOAP getBackLinkedWorkitems.
- **Polarion 2506** - not yet validated
  - REST custom-field configuration endpoints arrive in 2512; older releases use SOAP or SVN.
  - REST /metadata arrives in 2512; the version comes from unauthenticated signals.
  - REST Document and Page listing arrive in 2512; older releases list Documents via SOAP queryModules.
  - REST back-links endpoint arrives in 2512; older releases use SOAP getBackLinkedWorkitems.
- **Polarion 2512** - not yet validated
- **Polarion 2606** - not yet validated
  - 2606 runs on Tomcat 11 / Jakarta EE 11 and removed Groovy; server-side extensions must be rebuilt.

## Checks against real servers

| Release | Date | Server | What was verified |
|---|---|---|---|
| 2606 | 2026-11-06 | local install (localhost) | Login page buildId + 'Version 2606', SDK changes.txt 3.26.6, /rest/v1/definition (284 ops) without auth; REST 401 JSON without token; SOAP WSDLs served; SOAP envelope + sessionID header + 'Authentication failed' fault shape; /repo 401 Basic realm 'Subversion repository'. Authenticated capabilities NOT yet verified (needs a PAT). |
| 2606 | 2026-11-06 | local install, repository read via svnlook (read-only) | Phase 2 SVN configuration readers on project drivepilot: roles, user-roles, permissions.xml (both grant styles + customsets), projects.xml path mapping, enums (incl. type-specific), custom-fields, workflows (Work Item, Document, Test Run; conditions/functions/signatures), read-only fields and form filters. SOAP WSDL types used by the readers (EnumOption, CustomFieldType, WorkflowAction, Module, Project) read from the live WSDL. |

Not yet checked on a real server: 2410 (REST off), 2506, 2512; any authenticated capability on 2606.
