# Polarion ALM Validation Studio: Technical Design

*Automated Testing. Configuration Validation. Version Compatibility.*

Version 0.1 (Phase 1), 2026-11-06. This document is the design baseline. Every later phase extends it and must not break it.

---

## 1. Goals and non-goals

**Goal.** A Windows desktop application that takes business requirements, works out what each one means in Polarion terms, inspects the real configuration of a running Polarion server, runs validation tests, and produces an evidence-backed report. It must keep working across Polarion releases without code changes for each release.

**Non-goals.**
- It is not a Polarion admin tool. It never changes configuration. The only writes it makes are test data inside approved test projects.
- It is not a generic REST tester. Requests are a means to validate requirements.
- It uses **no AI or language models anywhere**. Test cases come from fixed, documented rules or from a structured specification; results only come from executed checks with evidence.

## 2. Facts this design is built on

Each of these was checked against the SDK or the local Polarion 2606 server on 2026-11-06. The design depends on them.

| # | Fact | Consequence |
|---|---|---|
| F1 | Real releases are **2410 (3.24.10), 2506 (3.25.6), 2512 (3.25.12), 2606 (3.26.6)**. The SDK REST change log and the Polarion blog list no 2503 or 2603 release. | Adapters exist only for real releases. An unknown release falls back to the nearest lower profile plus live probing (§6). |
| F2 | REST is **off by default** (`com.siemens.polarion.rest.enabled`). A locked-down client server (2410) may have it off and must keep it off. | REST cannot be the only channel. **SOAP is a first-class provider.** |
| F3 | REST covers no permission configuration, no workflow definitions, no baselines, no history, no Velocity templates (REST guide "Limitations"). `GET /roles/{id}` returns only a global role. | Configuration inspection needs pluggable providers: SOAP, a read-only SVN reader, behavioural tests, and later the browser (§5). |
| F4 | SOAP `SecurityWebService` has `hasPermission(user, permission, project)`, `getRolesForUser`, `getContextRoles`, `canReadInstance/canModifyInstance/canDeleteInstance(uri)`, `canModifyKey(uri, field)`. | Permission validation can ask the server directly instead of guessing from config. |
| F5 | The SOAP endpoint is `…/polarion/ws/services/<Service>WebService`. The namespace is `http://ws.polarion.com/<Service>WebService-impl`. The session id comes back in header `{http://ws.polarion.com/session}sessionID`, even on anonymous calls. A failed login returns a SOAP Fault "Authentication failed". | Verified live. The SOAP client builds its own envelopes over the shared HTTP transport (§7.3). |
| F6 | The login page is served without authentication and contains `buildId=20260720-0945-2606.2607-…` and the text "Version 2606". `/polarion/sdk/doc/rest/changes.txt` starts with `Version 3.26.6`. `/polarion/rest/v1/definition` serves the live OpenAPI spec (200 without a token on 2606 with Swagger on). `GET /metadata` (since 2512, authenticated) returns version and build. | Version can be detected without credentials, from several independent signals (§6.1). |
| F7 | Without a token, every `/rest/v1/*` path returns 401 JSON, including paths that don't exist. | Endpoint probing only works with a token. Without one, capabilities come from the documented catalog only. |
| F8 | Configuration lives in the SVN repository at `/repo` (Basic auth with Polarion credentials): `.polarion/security/permissions.xml`, `.polarion/security/user-roles.xml`, `.polarion/tracker/workflow/<type>-workflow.xml`, `.polarion/tracker/fields/*-enum.xml`, `*custom-fields.xml`, `.polarion/pages/…`, `.polarion/scripts/`. | An explicitly configured, read-only SVN provider can read the real XML (§5). |
| F9 | REST endpoints by release: 76 before 2310, then 113 / 20 / 12 / 0 / 50 / 13 for 2310 / 2404 / 2410 / 2506 / 2512 / 2606. One was removed in 2506 (`POST …/testruns/{id}/actions/exportTestsToExcel`). Custom-field config, `/metadata`, `/enumerations` listing and `getFieldsMetadata` exist only from 2512. | The capability registry is generated from the spec and change log, not typed by hand (§6.2). |

## 3. Architecture overview

```
                       ┌────────────────────────── UI (PySide6) ───────────────────────────┐
                       │ Dashboard · Environments · Requirements · Tests · Inspector ·     │
                       │ Compatibility Center · Reports        (no business logic here)    │
                       └───────────────▲───────────────────────────────────────────────────┘
                                       │ application services (sync API, run in worker threads)
┌──────────────────────────────────────┴─────────────────────────────────────────────────────┐
│ Services: EnvironmentService · ConnectionService · InspectionService · RunService · Report │
└───▲────────────────▲─────────────────────▲──────────────────────▲──────────────────▲───────┘
    │                │                     │                      │                  │
┌───┴─────┐  ┌───────┴────────┐  ┌─────────┴─────────┐  ┌─────────┴────────┐  ┌──────┴──────┐
│ Require-│  │  Inspection    │  │   Testing         │  │ Validation &     │  │ Reporting   │
│ ments   │  │  Engine        │  │   Engine          │  │ Evidence Engine  │  │ XLSX/HTML/  │
│(rules,  │  │(reads config   │  │(executes checks,  │  │(expected vs      │  │ CSV, history│
│ no AI)  │  │ via providers) │  │ guarded writes)   │  │ actual, verdict) │  │             │
└───┬─────┘  └───────┬────────┘  └─────────┬─────────┘  └─────────┬────────┘  └─────────────┘
    │                └───────────┬─────────┘                      │
    │                ┌───────────┴──────────────────────────────┐ │
    │                │ Polarion Core Engine                     │ │
    │                │  Version Detection · Capability Registry │◄┘ evidence of every exchange
    │                │  Version Adapter Manager · Write Guard   │
    │                │  Providers: REST · SOAP · SVN · Browser  │
    │                │  HTTP Transport (TLS, timeouts, retries, │
    │                │  redaction, exchange recorder)           │
    │                └──────────────────────────────────────────┘
    └─ Requirement rules: fixed patterns (no AI, no network)
Infrastructure: Settings · Credential Vault (Windows Credential Manager) · SQLite store · Audit log · Logging with secret redaction
```

**Layering rule.** Dependencies point downwards only: `ui → services → engines → polarion core → infra`. `domain` (Pydantic models and enums) is shared by all. The UI never imports `polarion.*`. Engines never import Qt.

## 4. Domain model (shared vocabulary)

| Concept | Meaning |
|---|---|
| `Environment` | A saved Polarion instance: name, base URL, `kind` (development / test / staging / production), TLS settings, timeout, auth method, credential reference, allowed **test projects**, which providers are enabled. |
| `ServerProfile` | The result of connecting: detected release + confidence + signals, which providers are reachable, resolved capabilities, current user, discovered projects. |
| `Release` | A Polarion release such as `2410`, with the internal version `3.24.10`. Parsed generically (`YYMM ↔ 3.YY.M`). |
| `Capability` | A named feature, e.g. `workitems.read`, `customfields.definitions`, `permissions.check`. |
| `CapabilityStatus` | `SUPPORTED`, `PARTIAL`, `UNSUPPORTED`, `NEEDS_PERMISSION`, `UNKNOWN`. |
| `Verification` | How we know: `DOCUMENTED` (SDK/spec says so), `DETECTED` (live spec or probe on this server), `VERIFIED` (a real call returned the expected data, with evidence). The matrix only says "supported on release X" once a `VERIFIED` record exists for X. |
| `Requirement` | Imported business requirement plus acceptance criteria (Phase 3). |
| `TestCase` | An executable check linked to a requirement. Origin: `RULE_GENERATED` or `MANUAL` (from a structured spec). Only an approved case runs. |
| `TestResult` | `PASS`, `FAIL`, `PARTIAL_PASS`, `BLOCKED`, `NOT_TESTABLE` (+ `ERROR` for crashes in the tool itself). |
| `Evidence` | Recorded HTTP/SOAP exchanges (redacted), parsed config extracts, screenshots, log excerpts, each with a hash and timestamp. |

**Result semantics.**
- `PASS`/`FAIL` need at least one executed check with evidence.
- `PARTIAL_PASS`: some acceptance criteria passed, others could not be checked or failed in a minor way.
- `BLOCKED`: the check could run in principle but something is in the way (missing permission, no test account, no test project, a provider is off).
- `NOT_TESTABLE`: no available mechanism can observe the behaviour (for example, a UI-only setting with browser testing off).
- Only an executed check with evidence can set any of these.

## 5. Inspection providers (pluggable)

Every way of looking at Polarion is a provider with the same contract: `is_configured()`, `probe() -> ProviderStatus`, and typed read operations. Providers are tried in a priority order that each feature defines, and only if the user enabled them for the environment.

| Provider | Channel | Default | Used for |
|---|---|---|---|
| **REST** | `/polarion/rest/v1` + PAT/JWT | On if reachable | Work Items, Documents and parts, links, workflow actions per item, enumerations, users, projects, test runs; custom-field definitions (2512+) |
| **SOAP** | `/polarion/ws/services` + password or PAT | On if reachable | Permission checks (`hasPermission`, `can*Instance`), roles per user, custom-field definitions on older releases, baselines, history, SQL queries, Document PDF export |
| **SVN config reader** | `/repo` WebDAV GET (read-only) | **Off**; needs explicit opt-in per environment | Real XML of permissions, user roles, workflows, fields, enumerations, form layouts, page/widget sources, scripts |
| **Browser** (Phase 5) | Playwright, test account | Off; opt-in | LiveReport rendering, UI-only settings (form filters, read-only fields), screenshots |
| **Database** (Phase 5, optional) | Read-only PostgreSQL connection | Off; opt-in | Large-volume traceability and coverage queries |

**Read-only enforcement.** The SVN reader uses GET/PROPFIND only, and the transport rejects any other method for it. The DB provider must connect with a read-only role, and it is checked at connect time.

## 6. Version compatibility architecture

### 6.1 Version detection

`VersionDetector` gathers **signals**. Each has a source, a value and a confidence weight. It reports the release with the highest total confidence plus every signal, so the Compatibility Center can show why it decided.

| Signal | Needs auth | Confidence |
|---|---|---|
| `GET /rest/v1/metadata` → version/build (2512+) | REST token | 1.0 |
| Login page `buildId=YYYYMMDD-HHMM-<release>.<build>-<hash>` | No | 0.9 |
| Login page text "Version NNNN" | No | 0.8 |
| `/polarion/sdk/doc/rest/changes.txt` first `Version 3.YY.M` line | No | 0.7 (the SDK could be stale after a partial upgrade) |
| REST endpoint fingerprint: the live `/rest/v1/definition` endpoint set matched against the catalog gives a *minimum* release | No (if served) | 0.5 |

If signals disagree, the conflict is shown as a warning and the highest-confidence value wins. If nothing is found, the release is `UNKNOWN`, and only providers and capabilities that were actually probed are trusted.

### 6.2 Endpoint catalog and capability registry (data, not code)

- `data/endpoint_catalog.json` is **generated** by `tools/build_endpoint_catalog.py` from the newest OpenAPI spec and the SDK REST change log. Each operation gets `since`, `removed_in` and `modified_in[]`. Regenerate it when a new release ships and the code stays the same.
- `data/capabilities.yaml` defines each capability:
  - id and title
  - module area (roles, workitems, workflow, documents, reports, traceability, customization)
  - **strategies**: an ordered list of `{provider, requires: [endpoints | soap ops | svn paths], coverage: full|partial}`
  - notes on limitations
- **Resolution per environment.** For each capability, take the first strategy whose provider is enabled and reachable and whose requirements exist:
  1. On the live endpoint set (`/definition`, if served), else
  2. In the catalog for the detected release.
  
  Then optional authenticated probes refine the status: 2xx → `SUPPORTED`/`VERIFIED`; 403 → `NEEDS_PERMISSION`; 404 on the route → `UNSUPPORTED`. A partial-coverage strategy gives `PARTIAL`. No strategy left → `UNSUPPORTED`.
- **Capability detection wins over version numbers.** The release only picks the default catalog view when nothing can be probed.

### 6.3 Version adapters

Adapters hold **behaviour differences**, not endpoint lists (those live in the catalog). `BaseAdapter` implements the newest known behaviour. A release adapter overrides only what differs and declares known quirks (for example, 2410: save hooks can't be scoped to a type; no `/customfields`; no `/metadata`).

- Registered adapters: `Adapter2410`, `Adapter2506`, `Adapter2512`, `Adapter2606`, `FutureAdapter`.
- **Selection:** the exact release match, else the nearest lower known release, else `FutureAdapter` (newest behaviour + probe everything + "unvalidated release" warning).
- 2503 and 2603 are **not registered** because they don't exist (F1). If a server ever reports one, it goes through nearest-lower selection and is flagged unvalidated.
- **Adding a release** means:
  1. Regenerate the catalog.
  2. Add a small adapter class only if behaviour differs.
  3. Run the compatibility suite against a real server.
  4. Commit the `VERIFIED` records.

### 6.4 Compatibility matrix

[COMPATIBILITY.md](COMPATIBILITY.md) is rendered from the registry and the stored verification records. Each cell shows status and verification level. "Supported" is only printed after a `VERIFIED` run on that release (instruction 15).

## 7. Polarion Core Engine details

### 7.1 HTTP transport (single choke point)

- `httpx.Client` per environment, with:
  - TLS verify on by default; an optional custom CA bundle (corporate CAs); "insecure" is allowed only for development environments, with a visible warning.
  - Connect/read timeouts, proxy support, HTTP/1.1.
- Retries with exponential backoff on connect errors, 502/503/504 and 429 (honouring `Retry-After`). **Never on non-idempotent requests.**
- Every exchange becomes an `Exchange` record: method, URL, status, duration, request/response headers and body excerpt. The recorder **redacts** `Authorization`, `X-Polarion-REST-Token`, cookies, SOAP `password`/`token` elements and the `sessionID` value before anything is stored or logged. Records feed the Evidence Engine.
- The **Write Guard** sits in the transport. Any non-read call needs a `WriteAuthorization` issued for (environment, project, operation, run id). The guard refuses on `production` environments unless there is an explicit per-run production approval, and refuses outside the environment's allowed test projects. Read-only is the default, and the guard is the only place that can lift it.

### 7.2 REST client

JSON:API helpers:
- Sparse fields, includes, paging iterator (follows `links.next`, pins `revision` when listing everything), sort and query.
- Error parsing (`errors[].status/title/detail/source`) and typed exceptions.
- Version-dependent paths come from the adapter.

### 7.3 SOAP client

Hand-built document/literal envelopes over the same transport, so TLS, timeouts, redaction and evidence are identical to REST.
- The session header is captured from responses and sent back.
- `logIn` / `logInWithToken("AccessToken", "", PAT)`; `endSession` on close.
- A generic `call(service, operation, params)` → parsed dict, with SOAP Faults mapped to typed errors.

Zeep was rejected for now: it would bypass the shared transport and recorder, and we only need a few dozen operations. This can be revisited if complex types get painful.

### 7.4 Authentication

| Method | REST | SOAP | SVN |
|---|---|---|---|
| Personal Access Token | `Authorization: Bearer` | `logInWithToken("AccessToken", "", token)` | n/a |
| Username + password | not supported by Polarion REST | `logIn(user, password)` | Basic auth |
| JWT (Phase 5) | Bearer | n/a | n/a |

The auth manager turns an environment's credentials into per-provider authenticators and reports which providers each credential can open.

### 7.5 Connection manager

`connect(environment)` →
1. Reachability of the base URL.
2. Version detection (no auth).
3. Provider probes: REST (401 JSON = present and enabled; 404/HTML = disabled), SOAP WSDL, SVN (401 Basic realm = present).
4. Authentication per provider.
5. Current user.
6. Capability resolution.
7. Project discovery.
8. Store a `ServerProfile` snapshot with all evidence.

Every step reports progress and partial results. A failing step doesn't hide the others.

## 8. Engines (later phases, interfaces fixed now)

- **Inspection Engine (Phase 2, built).** See §8.1.
- **Testing Engine (Phase 4).** Check types: `ConfigAssertion` (inspected value vs expected), `PermissionProbe` (SOAP `hasPermission` + behavioural attempt with a test account), `WorkflowRun` (create a test item in a test project → run actions → assert status/fields → clean up), `TraceabilityQuery`, `BrowserCheck`, `StaticAnalysis`. A check is idempotent where possible and always records evidence. Runs have timeouts, retries for transient errors, cancellation, and resume from the last finished check.
- **Requirements & test design (Phase 3, rule-based, no AI).** Requirement text is imported (txt/csv/xlsx/docx/pdf/json/yaml), acceptance criteria are split out by fixed patterns, and each sentence is matched against a documented list of rules (role exists, permission granted/denied, field required, status transition allowed, ...). Names are resolved against the inspected configuration. A sentence no rule understands becomes a finding ("not automatable"), never a guessed test.
### 8.2 Phase 4 safety design (writes)

`engines/testing/behaviour.py` is the only code that writes, and only through the Write Guard (section 7.1): (1) the project must be in the environment's `test_projects` (an explicit, audited user approval), (2) production also needs the typed name, (3) a per-run `WriteAuthorization` is issued and revoked afterwards, (4) the transport refuses any write aimed at another project. Created items are titled `[PAVS-<run>]`, recorded in `test_artifacts` immediately, capped per run, and deleted in a `finally` block; deletion re-reads the item and only deletes if the tag is still in the title. REST delete is `DELETE /projects/{p}/workitems` with a list body. Behavioural permission tests sign in as a test account (token in the vault, never in the database) and treat a REST *endpoint* denial as BLOCKED, so a missing REST permission can never look like a successful permission test.

- **Validation & Evidence Engine (Phase 4).** Compares expected and actual, computes verdicts per acceptance criterion and per requirement, records defects/discrepancies, and keeps evidence hash-linked.
- **Reporting (Phase 6).** HTML (Jinja2), PDF (ReportLab), Excel (OpenPyXL), CSV. Historical comparison between runs.

### 8.1 Inspection Engine (Phase 2)

**Principle: APIs give the effective configuration, SVN files give the layers.** REST/SOAP answer "what does Polarion apply here?". The SVN files answer "where does it come from?" (global vs project, generic vs type-specific). Each item carries its `sources`, each section has a status (complete / partial / unavailable / error), and anything that could not be read becomes a **gap**. A gap records what is missing, why, and how to make it readable. A gap is never a guess.

Code: `pavs/engines/inspection/`. `parsers.py` (pure XML parsers), `repo.py` (SVN paths + project path from `/.polarion/projects.xml`), `enums.py` (one enumeration via REST → SOAP → SVN), `readers_*.py`, `engine.py`. Results are `ConfigSnapshot`s (`pavs/domain/config.py`), stored per environment + project (table `config_snapshots`) and comparable (`services/inspection.py: diff_snapshots`).

| Section | REST | SOAP | SVN (opt-in) |
|---|---|---|---|
| Work Item types, link roles | enumeration endpoint | `getAllEnumOptionsForId` (ids/names only) | `workitem-type-enum.xml`, `workitem-link-role-enum.xml` |
| Custom fields | effective per type (2512+) | `getDefinedCustomFieldTypes` per type | `custom-fields.xml`, `<type>-custom-fields.xml` (both scopes) |
| Enumerations | list (2512+) | per id (well-known + field-referenced) | all `*-enum.xml` in tracker/documents/testing/planning, project file wins |
| Read-only fields, form filters | — | — | `hats/<view>/tracker/*-read-only-fields.xml`, `*-form-filter.xml` (UI only) |
| Workflows | per item: `getWorkflowActions` | per item: `getAvailableActions` / `getUnavailableActions` | definitions; effective per type: project+type → global+type → project → global |
| Roles, assignments | — | `getGlobalRoles`, `getContextRoles(location)`, `getRolesForUser` | `roles.xml`, `project-roles.xml`, `user-roles.xml` (users and groups) |
| Permissions | — | `hasPermission` (effective, per user) | `permissions.xml` both scopes: both grant styles, `@customset` refs |
| Documents, doc types/statuses | list (2512+), enumerations | `queryModules` | `documents/fields/*-enum.xml` |
| Pages | list (2512+) | `getWikiSpaces` + `getWikiPages` | — |

**Per-item workflow sampling.** For each Work Item type (max 15), one existing item is read (`queryWorkItemUrisLimited` / REST query with `page[size]=1`) and its actions are listed. This is read-only and gives real workflow behaviour even without SVN, for example on a locked-down 2410 server. It can be switched off in the UI.

**Verified on real data (2026-11-06, local 2606 `drivepilot`, read-only via `svnlook`):**
- 12 types, 16 link roles, 37 field definitions
- 54 enumerations, 29 of them type-specific
- 52 UI rules
- 23 effective workflows: 12 Work Item, 7 Document, 4 Test Run
- 208 permission rules with 2 custom sets
- 24 assignments

The REST/SOAP paths were verified against the mock only (the real check needs a token).

### Worked example: "Internal Reviewer may review but not modify or delete Work Items"

| Criterion | Mechanism | Provider | If unavailable |
|---|---|---|---|
| Role `internal_reviewer` exists in project P | `getContextRoles(P)`; or `user-roles.xml` | SOAP / SVN | `BLOCKED` |
| Test account T has the role | `getRolesForUser(T, P)` | SOAP | `BLOCKED` (needs a test account) |
| T may comment/approve (review) | `hasPermission(T, <review perm>, P)` + behavioural: as T, add a comment to a test item | SOAP + REST (as T) | `PARTIAL_PASS` if only one side runs |
| T may not modify | `canModifyInstance(uri)` as T + behavioural PATCH expecting 403 | SOAP + REST (as T), in the test project only | `BLOCKED` without a test project |
| T may not delete | `canDeleteInstance(uri)` as T + DELETE expecting 403 | SOAP + REST (as T), in the test project only | same |
| Matches `permissions.xml` | Parse the permission grants for the role | SVN (opt-in) | skipped, noted |

Exact permission names come from `permissions.xml` and the Help, not from guesses. This is validated in Phase 2.

### 8.3 Phase 5/6 additions (as built)
- **Browser checks** (`engines/testing/browser.py`): Playwright drives the installed Edge/Chrome, headless, throw-away profile; B01 login page, B02 sign-in, B03 project page, B04 JavaScript errors. Opt-in per run; BLOCKED with a remedy when Playwright or a browser is missing. Not a connection provider.
- **Merge preparation** (`services/merge.py`): compares two projects' `.polarion` trees through the read-only repository reader (same or two servers), checks the files that would arrive (`safexml` well-formedness, script/Velocity rules against the TARGET release), saves the result as a `merge` run so every export works. Two-way only: no common ancestor, so real conflicts are TortoiseSVN's job.
- **TortoiseSVN hand-off** (`integrations/tortoisesvn.py`): builds documented `TortoiseProc.exe` command lines (repobrowser, log, blame, showcompare, diff, checkout, merge, update, commit) and `TortoiseMerge.exe` for two local files. URLs and paths are validated (no quotes/control characters, only http/https/svn/file URLs), arguments are never passed through a shell, credentials never appear on a command line. Looking is `Tester`; checkout/merge/commit need `Lead`, production needs `Admin`. The spawn function is replaced in tests, so no test starts a real program.
- **Roles** (`services/roles.py`): Viewer < Tester < Lead < Admin, stored in the local database, enforced in the services (not only the UI), raising the role can require a salted PBKDF2 passphrase. A guardrail, not a security boundary.
- **CLI** (`cli.py`, `pavs-cli`): scripts, projects, run, compat, merge-check; secrets only from environment variables; exit codes 0/1/2.
- **Hardening:** `infra/safexml.py` refuses any DOCTYPE/ENTITY in server or repository XML; every export scrubs secrets and neutralises leading `= + - @` in spreadsheet cells; JWT bearer tokens are used for REST only.
- **Packaging:** `packaging/pavs.spec` builds one PyInstaller folder with the GUI and CLI exes; data files (`capabilities.yaml`, `endpoint_catalog.json`, `api_methods.json`) are bundled; Playwright only with `PAVS_BUNDLE_PLAYWRIGHT=1`.

## 9. Safety and security

- **Read-only by default.** Writes only through the Write Guard (§7.1), only in allowed test projects, and only after a run-level approval dialog that lists what will be created or changed and how it is cleaned up.
- **Production:** writes are blocked outright. The override needs the environment kind changed or a typed confirmation per run, and both are audit-logged.
- **Credentials:** Windows Credential Manager via `keyring` (DPAPI, per Windows user). The database stores only a credential reference. If no secure backend exists, the app asks each session and stores nothing. **Never written to logs, evidence or reports:** the redaction filter is applied in the transport *and* in the logging handler.
- **Audit log:** append-only SQLite table (who, when, environment, action, target, outcome) for connects, credential changes, approvals, writes and exports.
- **App roles (Phase 6):** Viewer (see reports), Tester (run read-only suites), Lead (approve write runs, production overrides), Admin (environments, credentials). Mapped to the Windows user.
- **Test data isolation:** every created artifact carries a run tag (e.g. title prefix `[PAVS-<run>]` + a custom field if present) and is cleaned up at the end. The cleanup list is persisted, so a crashed run can be cleaned later.

## 10. Persistence

SQLite with WAL, in the data directory (`PAVS_DATA_DIR`, default `%LOCALAPPDATA%\PolarionValidationStudio`). The repository classes sit behind interfaces, so a PostgreSQL implementation can be added for a central deployment (Phase 6). Tables: `environments`, `server_profiles`, `capability_records`, `audit_log`; later `requirements`, `test_cases`, `runs`, `results`, `evidence`. Schema migrations go through a numbered `PRAGMA user_version` scheme.

## 11. Technology decisions

| Decision | Choice | Why |
|---|---|---|
| UI | **PySide6 (Qt 6)** | Native Windows look and speed, a single language with the engines, and good packaging. Electron would add a Node toolchain and a second language for no benefit here. |
| Backend shape | In-process service layer, **no FastAPI** in the desktop build | A local HTTP server adds an attack surface and moving parts. The service layer is plain Python, so a FastAPI wrapper can be added later for a central deployment without touching the engines. |
| HTTP | **httpx** (sync client in Qt worker threads) | One transport for REST, SOAP and SVN, with retries/recording. Async isn't needed: concurrency is bounded per environment and the UI stays responsive through `QThreadPool`. |
| SOAP | Own envelopes on httpx | Shared evidence/redaction/TLS. Verified against 2606 (F5). |
| Models | **Pydantic v2** | Validation and serialization for config, evidence and reports. |
| Storage | **sqlite3** (stdlib) behind repositories | Zero extra dependencies. PostgreSQL later. |
| Secrets | **keyring** → Windows Credential Manager | OS-level encryption, no custom crypto. |
| Config data | YAML for hand-edited capabilities, JSON for the generated catalog | Readable and diff-friendly. |
| Tests | **pytest** + a WSGI mock Polarion (version profiles) via `httpx.WSGITransport` | No sockets, fast, deterministic. The same mock can be served for UI work. |
| Browser (Phase 5) | Playwright | Reliable, has screenshots. |
| Reports (Phase 6) | Jinja2 HTML, ReportLab, OpenPyXL | As requested. |
| Packaging (Phase 6) | PyInstaller (one-folder) + optional MSI | The usual choice for PySide6 on Windows. |

**Machine constraint:** the C: drive is nearly full, so the virtual environment, pip cache and build output live on `D:\PolarionValidationStudio`. The source code stays in this folder.

## 12. Mock Polarion server

`mock_polarion` is a dependency-free WSGI app with **profiles** that mirror real releases:
- `2410-rest-off`: SOAP only, like a locked-down client server
- `2410`
- `2506`
- `2512`
- `2606`

It serves:
- a login page with the right `buildId`
- `changes.txt`
- `/rest/v1/definition` filtered to that release's endpoints
- JSON:API responses for the endpoints the tests need
- 401/403/404 behaviour as observed live (F7)
- SOAP Session/Security/Project operations with the real envelope shapes (F5)
- an SVN `/repo` with sample config XML

Profiles are built from the endpoint catalog, so the mock and the registry can't drift apart.

## 13. Folder structure

```
Polarion ALM Validation Studio/
├─ README.md                    how to install, run, test
├─ pyproject.toml               package + tool config
├─ run.ps1                      dev launcher (venv + data dir on D:)
├─ docs/
│  ├─ ARCHITECTURE.md           this document
│  ├─ COMPATIBILITY.md          capability matrix per release (documented vs verified)
│  └─ ROADMAP.md                phases, status, open questions
├─ src/pavs/
│  ├─ __main__.py               `python -m pavs` → desktop app
│  ├─ app.py                    composition root (wires infra → core → services → UI)
│  ├─ domain/                   models.py, enums.py, errors.py (no I/O)
│  ├─ infra/                    paths, settings, logging (redaction), vault, storage, audit
│  ├─ polarion/
│  │  ├─ transport.py           httpx wrapper, retries, recorder, redaction
│  │  ├─ safety.py              Write Guard
│  │  ├─ auth.py                credential → provider authenticators
│  │  ├─ rest/client.py         JSON:API client
│  │  ├─ soap/client.py         envelope client + session
│  │  ├─ svn/client.py          read-only config reader
│  │  ├─ version/               release.py (parsing), detector.py (signals)
│  │  ├─ capabilities/          catalog.py, registry.py, data/*.json|yaml
│  │  ├─ adapters/              base.py, releases.py, manager.py
│  │  ├─ connection.py          ConnectionManager → ServerProfile
│  │  └─ discovery.py           projects, current user (Phase 1); spaces, users, roles (Phase 2)
│  ├─ engines/                  inspection/ requirements/ testing/ (validation + reporting later)
│  ├─ services/                 environments.py, connections.py
│  └─ ui/                       main_window, theme, workers, pages/, dialogs/
├─ mock_polarion/               WSGI mock + release profiles + serve.py
├─ tools/build_endpoint_catalog.py
└─ tests/                       pytest suite (no network, no real credentials)
```

## 14. Phase plan

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **1 Foundation** (done) | Design, structure, transport, Write Guard, vault, storage/audit, auth, version detection, catalog + capability registry, adapters, connection manager, project discovery, mock server, unit tests, desktop shell (Dashboard, Environments, Compatibility Center) | Tests green; connects to the local 2606 and detects 2606 without credentials; with a PAT, lists projects and verifies capabilities |
| **2 Inspection** (done) | Readers for roles/permissions (SOAP+SVN), Work Item types, fields, enumerations, workflows (`getWorkflowActions` + SVN XML), documents; Configuration Inspector UI | Same readers return matching data on 2606 and on a 2410 profile (mock + client server) |
| 3 Requirements & tests | Import (txt/csv/xlsx/docx/pdf/json/yaml), criteria extraction, rule-based test generation, mapping to configuration, built-in project health checks, Connect page | Enter URL + credentials, pick a project, run tests, see pass/fail with evidence |
| **4 Execution** (done) | Data checks on real Work Items, guarded behavioural tests (workflow run on temporary items, permissions as test accounts), approval flow, artifact tracking, guaranteed cleanup | Internal Reviewer example runs end to end on a test project (mock-verified; real-server verification pending) |
| 5 Advanced | Browser checks, script/Velocity static analysis, regression, compatibility suite, JWT | Compatibility suite produces VERIFIED records |
| 6 Release | Reports, history comparison, app roles, packaging (PyInstaller/MSI), security review | Signed installer; reports reviewed |

## 15. Risks and open questions

1. **Exact permission ids** used by `hasPermission` (e.g. for comment/approve) need confirming from `permissions.xml` on a real server (Phase 2).
2. **Behavioural permission tests need test accounts** with known roles and their own PATs. The client must provide these. Without them, permission criteria are `BLOCKED`, never guessed.
3. **REST on the client (2410) is off.** All client testing goes through SOAP (+ SVN, if the client allows it). SOAP needs a password or PAT for each test account.
4. **`/rest/v1/definition` unauthenticated** was seen on 2606 with Swagger enabled. Behaviour with Swagger off is unknown, so the detector treats it as optional.
5. **Requirement wording:** rules understand a defined set of sentence patterns. Anything else is reported as not automatable instead of being interpreted; the documented patterns are in `docs/REQUIREMENT-PATTERNS.md`.
