# Testing guide: how to test everything yourself

Work through the parts in order. Parts 1 to 8 need no real Polarion login (they use the bundled **mock Polarion** with invented data). Part 9 is the careful, staged test on a real server. Tick each box as you go; "Expected" says what you should see.

All commands are PowerShell, run from the project folder:
`C:\Users\<you>\Desktop\Claude\Stand Alone Applications\Polarion ALM Validation Studio`

The Python environment is `D:\PolarionValidationStudio\venv`. Shortcut used below:

```powershell
$py = "D:\PolarionValidationStudio\venv\Scripts\python.exe"
```

---

## 0. The one-command check

```powershell
qa check                                   # every scenario on mock servers (about 7 minutes)
qa check --real-local --exe D:\PavsBuild\dist\PolarionValidationStudio   # plus the real local Polarion (no login) and the executables
```

It runs 47 scenarios over real HTTP against all five release profiles: connections and failures, every requirement file format, behavioural runs and production safety, roles, history, merge and TortoiseSVN hand-off, exports, secrets in logs and database, the GUI pages, the command line, the single-window behaviour, browser checks, the executables, and (with `--real-local`) your real repository read through svnlook. Each line says PASS or FAIL with the reason; any FAIL makes the command exit 1.

## 1. Automated tests (2 minutes)

```powershell
& $py -m pytest tests -q
```

- [ ] Expected: all pass (222 tests), no failures. Six browser tests run only if Edge/Chrome and Playwright are present; otherwise they are skipped, which is fine.
- No test touches your real Polarion, Windows Credential Manager, your data folder, or starts TortoiseSVN.

Run one area only if something fails:

```powershell
& $py -m pytest tests/test_merge.py -q      # merge + TortoiseSVN
& $py -m pytest tests/test_roles.py -q      # roles
& $py -m pytest tests/test_browser.py -q    # real headless Edge against the mock
```

---

## 2. Start the mock Polarion

Open a **second** PowerShell window (leave it running):

```powershell
$py = "D:\PolarionValidationStudio\venv\Scripts\python.exe"
$env:PYTHONPATH = "src;."
& $py -m mock_polarion.serve 2606 --port 8810
```

Other profiles you can try later: `2410-rest-off` (SOAP only, like a locked-down server), `2410`, `2506`, `2512`.

Fake logins (mock only):

| What | Value |
|---|---|
| URL | `http://127.0.0.1:8810/polarion` |
| User / password | `admin` / `admin-test-pw` |
| Token (PAT) | `test-token-admin` |
| Projects | `acme_demo`, `acme_test` |
| Test accounts | role `internal_reviewer` user `tester1` token `test-token-tester`; role `project_assignable` user `dev1` token `test-token-dev` |

---

## 3. Start the app and test each page

Use a throw-away data folder so you do not mix test data with real use:

```powershell
$env:PAVS_DATA_DIR = "D:\PolarionValidationStudio\data-test"
$env:PYTHONPATH = "src;."
& $py -m pavs
```

(Or double-click **Start Polarion Validation Studio.bat**; it uses `D:\PolarionValidationStudio\data`.)

### 3.1 Connect & Test (the main page)
1. Polarion URL `http://127.0.0.1:8810/polarion`, Environment type *Development*, user `admin`, password `admin-test-pw`, Access token `test-token-admin`. Leave *Read configuration files too* ticked. Press **Connect**.
   - [ ] Expected: green "Connected to Polarion 2606 as admin."; the project list fills with `acme_demo` and `acme_test`; the line under it shows REST, SOAP and SVN as Authenticated.
2. Wrong password test: change the password to `nope`, remove the token, Connect.
   - [ ] Expected: a clear "Sign-in failed ..." message, no crash.
3. Reconnect correctly, choose project `acme_demo`, tick **Built-in health checks**, **Work Item data checks** and **Script analysis**, press **Run tests**.
   - [ ] Expected: a verdict (FAILED), about 36 checks. One FAIL is intended: "Velocity blocks are balanced" (the mock project has a broken template). Click a row: Expected / Actual / Evidence appear below.
4. Press **Run tests** a second time, then open **History**.
   - [ ] Expected: two runs listed; the result line says how many checks got worse/fixed since the previous run. *Compare with previous run* shows all differences.
5. **Export report...** once for each format: `.xlsx`, `.pdf`, `.html`, `.csv`.
   - [ ] Expected: all four open. Search each for `admin-test-pw` and `test-token-admin`: they must **not** appear.
6. Requirements: **Paste text...** and paste:
   ```
   Only the Approved status may be reached from Reviewed.
   Every Requirement must be linked to a Test Case.
   ```
   Tick *Requirement tests* and run.
   - [ ] Expected: generated checks listed under *Requirements & generated tests*; a traceability result like "x of y linked". Sentences the rules do not understand are listed as *not automatable*, never guessed.
7. **Folder...** / **Analyze files**: create a folder with a file `bad.js` containing `var password = "hunter22x";` and press **Analyze files**.
   - [ ] Expected: FAILED with rule S02 (secret), and the value `hunter22x` is **not** shown anywhere.

### 3.2 Behavioural tests (the only part that writes; mock only for now)
1. Tick **Behavioural tests**, choose project `acme_demo`, press **Run tests**.
   - [ ] Expected: a warning dialog explaining exactly what will happen, with **Allow and run** / **Cancel**. Press **Cancel**: nothing runs.
2. Press **Test accounts...**, add role `internal_reviewer`, user `tester1`, token `test-token-tester`; add `project_assignable` / `dev1` / `test-token-dev`.
3. Run again and choose **Allow and run**.
   - [ ] Expected: temporary Work Items titled `[PAVS-...]` are created, a workflow transition and role permissions are tried, then **all are deleted** at the end. Open the mock's data via the app (Data checks) or press **Clean up test data**: Expected "No test Work Items are recorded".
4. Set environment type to **Production** (Environments page, Edit) and try the behavioural run again.
   - [ ] Expected: you must type the exact environment name; a wrong name refuses to run.

### 3.2b Role Test (writes in a test project; real server only)

1. Connect on *Connect & Test* with an administrator (username + password), set your application role to **Lead**, then open **Role Test**.
2. Choose the environment and the TEST project and press *Load users and roles from Polarion*. Pick a user, tick one or more roles, enter that user's password.
3. Optional: *Load expected permissions (Excel)...* and choose the sheet. Leave it empty to expect that everything works.
4. Press *Run role test* and confirm. Expect the progress to go through: assigning the role, preparing test data, testing each Work Item type, documents, cleanup, restoring the roles.
5. Read the **Blockers report** tab (and *All checks*). *Export report...* saves it as Excel/PDF/HTML/CSV.
6. Check afterwards: the user has their original roles again (Polarion > Administration > User Management) and no Work Item or document titled `[PAVS-ROLE-...]` is left. If something is left, the banner on the page offers *Restore roles* / *Clean up test data*.

### 3.3 Dashboard
- [ ] Expected: *Saved environments* 1, *Reachable* 1/1, and the totals (Requirements, Checks, Passed, Failed, Blocked, Coverage) match the **latest** run of each project (not the sum of every run).

### 3.4 Configuration Inspector
1. Pick `acme_demo`, press **Inspect**.
   - [ ] Expected: sections (Work Item types, link roles, custom fields, enumerations, workflows, roles, permissions, documents, pages). Click each; tables fill.
2. Switch the mock to `2410-rest-off` (restart it with that profile), connect with user/password only (no token).
   - [ ] Expected: sections still fill through SOAP, and anything that could not be read is listed with the reason and the fix ("Could not be inspected"). No silent gaps.
3. **Check a permission**: user `tester1`, a permission name, press check.
   - [ ] Expected: GRANTED/DENIED from the server.
4. Inspect twice, then compare the two snapshots.
   - [ ] Expected: "no differences", or the changes if you edited something.

### 3.5 Merge & Templates (with TortoiseSVN)
1. Open **Merge & Templates**. Source `acme_demo`, target `acme_test` (same environment), press **Compare and check**.
   - [ ] Expected: a table of ADDED / CHANGED files with areas (pages, hats, security, tracker). `pages/widgets/demo/render.vm` shows a **V01** error ("#foreach is never closed"). The status line says problems in incoming files would break the target.
   - [ ] The line "TortoiseSVN found: C:\Program Files\TortoiseSVN\bin\TortoiseProc.exe" shows. If it says not found, install TortoiseSVN or set `PAVS_TORTOISE_DIR`.
2. **Export report...** (xlsx/pdf).
   - [ ] Expected: one row per differing file with status and the problems as evidence.
3. The TortoiseSVN buttons against the **mock** will open TortoiseSVN windows that then fail to read the fake repository. That is expected and harmless: what you are checking is that the right dialog opens (Compare in TortoiseSVN, Diff selected file, Log, Repository browser). **Check out target...** and **Merge in working copy...** ask for a folder first and then ask you to confirm.
4. The real merge test is in Part 9.5.

### 3.6 Compatibility Center
- [ ] *This server*: capabilities per area with Status and Verification (Detected / **Verified** = a real call worked). *Release matrix*: columns 2410, 2410 REST off, 2506, 2512, 2606. *Adapters & quirks* lists the version adapters.

### 3.7 Environments
- [ ] **Add environment**, **Edit**, **Delete**, **Test connection & detect version**. Detail pane shows version signals, providers and projects. Deleting needs the Admin role (see 3.8).

### 3.8 Roles (bottom of the sidebar)
1. Set the role to **Viewer**, then try **Connect**.
   - [ ] Expected: refused: "Your role (Viewer) cannot connect to Polarion... needs the Tester role".
2. **Tester**: connect and run read-only tests works; behavioural tests are refused ("needs the Lead role").
3. **Lead**: behavioural tests, test accounts and TortoiseSVN checkout/merge work.
4. **Admin**: everything; deleting environments and production changes work.
5. Passphrase (optional): in a Python shell or via the tests you can set one; after that, raising the role asks for it. Lowering the role never asks.

---

## 4. Command line

```powershell
$env:PAVS_DATA_DIR = "D:\PolarionValidationStudio\data-test"
$env:PYTHONPATH = "src;."
$env:PAVS_USER = "admin"; $env:PAVS_PASSWORD = "admin-test-pw"; $env:PAVS_TOKEN = "test-token-admin"
$u = "http://127.0.0.1:8810/polarion"

& $py -m pavs.cli projects --url $u
& $py -m pavs.cli run --url $u --project acme_demo --data --scripts --out "$env:TEMP\run.pdf"; $LASTEXITCODE
& $py -m pavs.cli compat --url $u --out "$env:TEMP\compat.md"
& $py -m pavs.cli merge-check --url $u --project acme_demo --target-project acme_test --out "$env:TEMP\merge.xlsx"; $LASTEXITCODE
& $py -m pavs.cli scripts "C:\path\to\a\folder\of\js-and-vm" --release 2410
```

- [ ] Expected: `projects` lists both projects; `run` prints FAILED with the failing checks and exit code **1**; `compat` writes a Markdown table of capabilities; `merge-check` prints "WOULD BREAK ..." lines and exit code 1; `scripts` on a clean folder exits **0**.
- [ ] Exit codes: 0 passed (or passed with gaps), 1 a check failed, 2 could not run (wrong password, missing credentials, role).
- [ ] Passwords come only from the environment variables; there is no `--password` option on purpose.

---

## 5. The packaged executables

The build is already at `D:\PavsBuild\dist\PolarionValidationStudio\`. To rebuild:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_exe.ps1 -Python $py -OutDir D:\PavsBuild
```

- [ ] Double-click `PolarionValidationStudio.exe`: the window opens with the title "Polarion ALM Validation Studio 0.5.0" and all six pages. Repeat 3.1 against the mock.
- [ ] `pavs-cli.exe --version` prints the version; `pavs-cli.exe scripts <folder>` works.
- [ ] Windows SmartScreen may warn because the exe is not code-signed: expected.
- [ ] Browser checks inside the exe report BLOCKED unless you built with `-BundlePlaywright`.

---

## 6. Browser checks (optional)

```powershell
& $py -m pip install playwright      # once; uses your installed Edge or Chrome, no download
```

1. In the app, connect to the mock with the **password**, tick **Browser checks**, run.
   - [ ] Expected: B01 Login page renders, B02 Sign-in works, B03 Project opens, B04 No JavaScript errors (B04 may be a warning: harmless).
2. Connect with only a **token** (no password) and run browser checks.
   - [ ] Expected: B01 passes, B02-B04 BLOCKED with "no username and password available".
3. Uninstall Playwright (`pip uninstall playwright`) and run again.
   - [ ] Expected: all four BLOCKED with the fix "pip install playwright".
4. Real server, no credentials needed for B01: connect to your local `http://localhost/polarion` and run only Browser checks.
   - [ ] Expected: B01 PASS (login form found in a real Edge).

---

## 7. Security spot-checks

- [ ] After a run with the mock, search the app log `…\data-test\logs\pavs.log` for `admin-test-pw` and `test-token`: no hits.
- [ ] Open `…\data-test\pavs.sqlite3` with any SQLite viewer: no password or token text in any table (the Credential Manager holds secrets only if you ticked *Remember credentials*).
- [ ] In an exported `.xlsx`, a Work Item title that starts with `=` shows with a leading `'` (formulas are neutralised).
- [ ] Tick **Verify TLS certificate** off only on purpose; it is on by default.
- [ ] Nothing is written to Polarion unless you ticked Behavioural tests **and** pressed Allow (test: after any normal run, the mock's Work Item count is unchanged).

---

## 8. Upgrade safety

- [ ] Start an older data folder (for example your existing `D:\PolarionValidationStudio\data`) with this version: saved environments and history are still there (the database upgrades in place).

---

## 9. The real server, step by step (do this carefully)

The app has **never** been run with real credentials. Test in this order and stop at the first surprise. Each stage only adds risk after the previous one passed.

### 9.1 Anonymous (no risk): your local Polarion 2606
- [ ] Connect page: URL `http://localhost/polarion`, any name, **no** password. Expected: reachable, version **2606** detected, "Sign-in failed / needs credentials" message, no crash.
- [ ] Run **Browser checks** only: B01 PASS.

### 9.2 Read-only with a Personal Access Token
1. In Polarion: your user menu, account settings, create a **Personal Access Token** (name it `pavs-test`).
2. In the app: URL, your user name, paste the token, **uncheck** *Read configuration files*, Connect.
   - [ ] Expected: REST Authenticated; projects listed (e.g. `drivepilot`).
3. Run **Built-in health checks** and **Work Item data checks** on one project.
   - [ ] Expected: results with evidence. Checks that need the repository say BLOCKED and why.
4. Open **Configuration Inspector** and inspect the project; compare with what you see in Polarion's own administration (types, workflows, custom fields). Note any difference.

### 9.3 Read-only with password and the SVN reader
- [ ] Connect with user name + password, *Read configuration files too* ticked. Expected: SVN "read-only access to /repo". Re-run health checks: the BLOCKED ones should now produce results. Run **Script analysis**: it reads the real templates.
- [ ] Compare against your own knowledge: do the findings point at real files and lines?

### 9.4 Browser checks on the real UI
- [ ] Connect with the password, tick Browser checks, run. Expected: B01-B03 PASS. If B02 or B03 FAIL, send me the evidence text: the login redirect and the project URL pattern are the parts I could only test on the mock.

### 9.5 Merge & Templates with TortoiseSVN (real)
1. Connect (password + configuration files). Merge page: source and target projects that really exist (for example two copies of the same project, or the 2410 dev project into a test project).
2. **Compare and check**. Expected: changed/added/removed files; incoming template problems shown with line numbers.
3. **Compare in TortoiseSVN**: the URL compare dialog opens (you sign in once; TortoiseSVN caches it). Double-click a file to see the diff.
4. **Diff selected file** and **Log of selected**: the right dialogs open for that file.
5. For a **real merge**: **Check out target...** into a new empty folder (TortoiseSVN checks out the target's `.polarion` folder). Then **Merge in working copy...**, choose that folder, confirm. TortoiseSVN's merge wizard opens: pick the source URL and revisions, review the changes and conflicts there, and **commit yourself** only if you are happy. The app never commits.
   - Do this first on a throw-away copy of a project, never directly on production.
   - [ ] Roles: with Tester the checkout/merge buttons are refused; with Lead they work.

### 9.6 Behavioural tests (writes): only in a dedicated TEST project
Never on a working or production project.
1. Create (or pick) a **dedicated test project** and a test user/token for each role you want to check (for example `internal_reviewer`).
2. Environment type **Test**. Tick Behavioural tests, press **Allow and run**.
3. During the run you can watch the project: items titled `[PAVS-<run>]` appear.
4. After the run:
   - [ ] Expected: no `[PAVS-...]` items remain. If any remain, press **Clean up test data**, and report it to me.
   - [ ] Expected: a permission result the account cannot get is BLOCKED or FAIL, never a false pass.
5. Items I could not verify without a real server (listed in `docs/ROADMAP.md`): whether `POST /workitems` accepts a status on create, the workflow action id used in `workflowAction=`, and the exact wording of REST permission errors.

### 9.7 The locked-down client server (2410, REST off)
- [ ] Connect with password only. Expected: REST Disabled, SOAP Authenticated, projects listed through SOAP. Health checks and the Inspector still work; REST-only features say BLOCKED with the reason. Use a test account and test project here too.

---

## 10. What to send me if something goes wrong

1. The exact text in the red/yellow message or the failing result row (Expected / Actual / Evidence).
2. The Polarion release and which providers were Authenticated (Environments page, detail pane).
3. The last lines of `…\data\logs\pavs.log` (secrets are scrubbed automatically; still glance through before sending).
4. Which role was selected in the sidebar.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `.ps1` cannot be run (execution policy) | `powershell -ExecutionPolicy Bypass -File <script>` |
| "TortoiseSVN was not found" | Install it, or set `PAVS_TORTOISE_DIR` to its `bin` folder |
| Browser checks all BLOCKED | `pip install playwright`, and make sure Edge or Chrome is installed |
| Everything BLOCKED on a server | Connect with a password and tick *Read configuration files too*; REST needs a PAT |
| "Your role (...) cannot ..." | Change the role in the sidebar (Admin for production and environment deletion) |
| JavaScript syntax checks missing | Install Node.js (or set `PAVS_NODE`); everything else still runs |
| C: drive full | Keep the venv, data folder and build output on D: (as set up) |
