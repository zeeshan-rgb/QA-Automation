# Setting up Polarion ALM Validation Studio on a client site

Do this in stages. Stage 1 is read-only and safe on any server. Stages 2 and 3 only happen after the client agrees in writing.
Read `docs/AUDIT-2026-11.md` first: it states what has been verified on a real server (Polarion 2606, read-only) and what has not
(REST, the write path, 2410/2506/2512).

## 1. What to ask the client for

| Need | Why | Notes |
|---|---|---|
| A Windows machine or VM that can reach Polarion over HTTPS | Where the app runs | Not the Polarion server itself. Windows 10/11, 4 GB RAM, 1 GB free disk. |
| The Polarion address, e.g. `https://polarion.client.com/polarion` | Connect page | Use the exact address the browser uses (https, not http). A page address is fine: the app cuts it back to the server address. |
| A **dedicated test account** with a role that can **read** the projects | Login | Never an administrator's personal account. The app is read-only by default, but a dedicated account makes the audit trail clear. |
| Network access to `/polarion/ws/services/` (SOAP) and, if REST is on, `/polarion/rest/v1/` | Providers | Ask whether REST is switched off (`com.siemens.polarion.rest.enabled=false`), as on some 2410 servers. SOAP alone is enough for most checks. |
| A **Personal Access Token** for that account (optional) | Turns REST on | Created by the account owner in Polarion. |
| **Read access to the repository** (`/repo`) with a user name and password (optional but strongly recommended) | Workflow, field, permission and template files; script analysis; Merge & Templates | On some servers this login differs from the Polarion login: enter it in *Repository user / password*. Without it, those checks say BLOCKED and explain why. |
| The corporate **CA certificate** (.pem), if the server uses an internal certificate authority | TLS | Environments page, Edit, CA bundle. Do not switch certificate checking off. |
| A **proxy** address, if the network requires one | Connectivity | The app honours the standard `HTTPS_PROXY` environment variable. PAC files and NTLM proxies are not supported yet. |
| (Stage 3 only) A **dedicated test project** and test users for the roles to be tested | Behavioural tests | Never a working or production project. |

## 2. Install on the client machine

Pick one:

**A. The packaged folder (no Python needed).** Copy the folder `PolarionValidationStudio` (contains `PolarionValidationStudio.exe`
and `pavs-cli.exe`) to, for example, `C:\Tools\PolarionValidationStudio`. Windows SmartScreen or antivirus may warn because the
executable is not code-signed: ask client IT to allow it (give them the file hash), or build it on the client machine (option B).

**B. From source.** Install Python 3.11+, then:
```powershell
git clone https://github.com/zeeshan-rgb/QA-Automation.git
cd QA-Automation
python -m venv venv
venv\Scripts\python -m pip install -e ".[dev,browser]"
venv\Scripts\python -m pavs
```

Optional extras (each only adds features, none is required):
- **TortoiseSVN** for the Merge & Templates hand-off (compare, diff, checkout, merge). Found automatically.
- **Microsoft Edge or Google Chrome** and `pip install playwright` for the browser checks. Nothing is downloaded: it drives the installed browser.
- **Node.js** for the JavaScript syntax check in script analysis (everything else works without it).

Data (database, logs) goes to `%LOCALAPPDATA%\PolarionValidationStudio`. Set `PAVS_DATA_DIR` to put it elsewhere (for example a
folder the client backs up). Passwords and tokens are kept in **Windows Credential Manager**, never in that folder.

## 3. First connection (stage 1, read-only)

1. Start the app. Role at the bottom of the sidebar: **Tester** is enough for stage 1 (set it with the selector; optionally set a role passphrase).
2. **Connect & Test**: paste the Polarion address, choose the **Environment type** (use *Production* for a live server: the app then refuses any write without the typed name), enter the user name and password, optionally the token and the repository login. Tick **Remember credentials** only on a machine the client accepts for that. Press **Connect**.
3. Expect `Connected to Polarion <release> as <user>`. Read the provider line:
   - `SOAP: Authenticated` is required. `REST: Available` means a token is needed to use it. `SVN: Authenticated` means the files can be read.
   - If the server redirects (http to https) or the certificate is not trusted, the message says exactly what to change.
4. Choose the project, tick **Built-in health checks**, **Work Item data checks** and **Script analysis**, press **Run tests**. Results are PASS / FAIL / PARTIAL / BLOCKED with evidence; BLOCKED always says what is missing.
5. **Export report...** (PDF, Excel, HTML, CSV) for the client. Reports contain your Windows user name and the server address, and never a password or token.

At this point nothing was written to Polarion. The audit log (database) records what was done.

## 4. Requirements and traceability (still read-only)

Load the client's requirements (Load file...: txt, md, csv, xlsx, docx, pdf, json, yaml; or Paste text...). Review the
**Requirements & generated tests** tab before running: sentences the rules do not understand are listed as *not automatable*,
vague wording as *ambiguous*, and exclusions ("except", "only") are reported rather than guessed.

## 5. Behavioural tests (stage 3, writes: only with written approval)

1. The client creates a dedicated test project and, for each role to test, a Polarion user holding that role plus a Personal Access Token for it.
2. In the app: **Test accounts...**: add each role, user and token. Approve the project (*Allow and run* dialog). On a production server the environment name must be typed.
3. Tick **Behavioural tests** and run. The app creates items titled `[PAVS-<run>]`, runs a workflow transition, tries the permissions as each test account, then **deletes everything it created** (even if the run fails). Check the test project afterwards; if anything is left, press **Clean up test data**.
4. Run this on a copy or a test environment first. The write path has not yet been verified on a real server (see the audit).

## 6. Repeating it: command line and scheduling

```powershell
$env:PAVS_USER="svc_pavs"; $env:PAVS_PASSWORD="..."        # never typed on the command line
pavs-cli.exe run --url https://polarion.client.com/polarion --project PROJ --data --scripts --out report.pdf
pavs-cli.exe merge-check --url https://polarion.client.com/polarion --project SRC --target-project DST --out merge.xlsx
```
Exit code 0 = passed, 1 = a check failed, 2 = could not run. Use a scheduler or build pipeline; keep the credentials in its secret store.

## 7. Before you leave the site
- [ ] Credentials: only the dedicated account; no tokens in chat, email, tickets or reports.
- [ ] Role and passphrase set if several people use the machine.
- [ ] Environment type correct (Production for live servers).
- [ ] Test-data cleanup confirmed (stage 3): no `[PAVS-...]` items remain.
- [ ] Reports reviewed for anything the client does not want to share (user names, server addresses).
- [ ] Anything you could not check is written down as BLOCKED or not verified, not as a pass.

## 8. If something does not work
| Message | What it usually means |
|---|---|
| `Sign-in failed ... SOAP: Authentication failed` | Wrong user/password; the stored password is out of date (type it again). |
| `The server redirects this address to https://...` | Use the https address. |
| `TLS/certificate problem` | Add the corporate CA bundle in the Environments dialog. |
| `SVN: Auth failed` / checks BLOCKED | The repository login differs from the Polarion login: use *Repository user / password*. |
| `REST: Available` only | A Personal Access Token is needed for REST. |
| Everything BLOCKED after a long idle time | Update to this version or later: it signs in again after a server session timeout. |
