# Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 Foundation | Design, transport, Write Guard, vault, storage + audit, auth, version detection, endpoint catalog, capability registry, adapters (2410/2506/2512/2606/Future), connection manager, project discovery, mock server, tests, desktop shell (Dashboard, Environments, Compatibility Center) | **Done 2026-11-06.** Anonymous detection verified on local 2606 |
| 2 Inspection | Readers for Work Item types, link roles, custom fields (effective + layers), enumerations, read-only fields/form filters, workflows (definitions + per-item actions), roles/assignments, permissions (+ server-side permission check), documents, pages. Snapshots with history and comparison. Configuration Inspector UI | **Done 2026-11-06.** 73 tests green. SVN readers verified on real 2606 `drivepilot` config. REST/SOAP readers verified on the mock only |
| 3 Requirements & test design | Import txt/csv/xlsx/docx/pdf/json; acceptance criteria; rule-based test generation (no AI); built-in project health checks; Connect page (URL + credentials, project picker) | **Done 2026-11-06.** 129 tests green. Verified end to end on the mock; health checks verified on the real 2606 `drivepilot` config; connect flow verified against the real local server (version detection + sign-in failure handling). Not yet run with real credentials |
| 4 Execution | Real Work Item data checks (link coverage, statuses, broken links, orphans); guarded behavioural tests (workflow transitions on real test Work Items, permissions tried as test accounts) with approval, Write Guard, audit and guaranteed cleanup | **Done 2026-11-07.** 150 tests green. Verified on the mock only: not yet run against any real Polarion server |
| 5 Advanced | Script/Velocity static analysis, regression between runs, browser checks (Playwright), compatibility suite, JWT, read-only DB, TortoiseSVN merge integration | **Done 2026-11-08.** Script analysis + regression verified on the user's real scripts and the real 2606 templates. Browser checks: login page verified on the real local 2606 with headless Edge, sign-in/project page on the mock only. `pavs-cli compat` writes the compatibility record. JWT goes to REST only. TortoiseSVN: compare/merge-check built and mock-tested, the real TortoiseSVN dialog start verified with TortoiseMerge. **Read-only DB provider deliberately not built** |
| 6 Release | PDF/Excel/HTML/CSV reports, history comparison, app roles, PyInstaller, security review | **Done 2026-11-08.** PDF export, roles, PyInstaller folder (GUI + CLI exes, launch tested), security review (untrusted-XML guard, formula-injection guard, secret scrub on every export, dependency audit). No MSI installer |

## Verified here vs. not verified
- **Verified on the real local Polarion 2606 (no credentials):** version detection, sign-in failure handling, repository file reading (via a read-only harness), 202 templates analysed, the web login form and its field names (`j_username`, `j_password`, `form#logInForm`) in a real headless Edge.
- **Verified with the real TortoiseSVN installed on this machine:** it is found, and TortoiseMerge starts with the generated command line. The URL compare / merge / checkout command lines follow TortoiseSVN's documented syntax but were not started (they need a working repository sign-in).
- **Not verified with real credentials:** REST/SOAP readers, behavioural tests, browser sign-in and project page, SVN-over-HTTP file listing of a project's `.polarion` tree, `merge-check` between two real projects. See the assumptions below.
- **Not built:** read-only database provider; MSI installer; a third-party-signed executable (Windows SmartScreen will warn on an unsigned exe).

## Still needed from you for real-server verification

1. **A Personal Access Token on the local 2606.** It is needed to verify the REST and SOAP readers against real responses. It is stored in Windows Credential Manager.
2. **Your OK to use the SVN reader** on the local server (Polarion username + password with repository read access), and later on the client.
3. **For a client 2410 server (REST off):** a test account (and ideally a dedicated test project) for SOAP access.

## Phase 4 assumptions to verify on a real server (cannot be checked without credentials)
- `POST /workitems` accepts `attributes.status` to create an item directly in the starting status of a transition. The REST guide says the initial workflow action is not triggered on create; if a server rejects `status`, the transition test reports BLOCKED with the server's message.
- `PATCH ...?workflowAction=` takes the workflow action's id (`nativeActionId`), as the REST guide states.
- A REST **endpoint** denial is recognised by the text "necessary permissions to perform this operation" (from the REST guide); other 403 texts are treated as item-level denials. If a server words these differently, a test may be reported FAIL/PASS instead of BLOCKED.
- REST item delete is `DELETE /projects/{p}/workitems` with a list body (there is no single-item DELETE endpoint; checked in the 2606 spec).
- Reading all Work Items for the data checks uses SOAP `queryWorkItemsLimited` (types verified against the live WSDL) or REST relationship ids.

## Open questions

- **REST custom-field type mapping.** The REST response field `type.kind`/`enumName` mapping comes from the REST guide example and is unverified on a real server. Ditto `multiValued`.
- **SOAP `getContextRoles(location)`.** It takes the project `location` returned by `getProject`. The exact format the 2410/2606 server expects is to be confirmed with a token.
- **Workflow `required roles`.** The parser accepts `<roles><role .../></roles>` inside an action. No real file with role restrictions was found in the demo repository, so this is unverified.
- **`hasPermission` ids** for review/comment/approve. The permission names are now readable from `permissions.xml` (Inspector → Permissions).
- Whether `/rest/v1/definition` is served when the Swagger UI is off.
