# Script and template analysis: what is checked

Static analysis only: scripts and templates are **read, never executed**. No AI. Each finding names the file, the line and why it matters. The rules come from bugs documented in the user's own scripting notes (`Polarion-Scripting-Knowledge-Base.md`) and from Siemens' SDK change log. Code: `src/pavs/engines/analysis/analyzer.py`; every rule is covered by `tests/test_phase5.py`.

**Where the files come from:** the repository (read-only SVN reader: widget packages `pages/widgets/*/*.vm`, Velocity macros `pages/scripts/velocity`, `scripts/`, form extensions under `testing/configuration`, and the Velocity inside every page's *Script* blocks) and/or a local folder of `.js` / `.vm` files (*Folder...* button), or a folder analysed offline with *Analyze files* (no Polarion connection needed). `.js.txt` and `.vm.txt` are treated as scripts and templates.

| Rule | Severity | Checks | Why |
|---|---|---|---|
| S01 | error | JavaScript syntax (needs Node.js; a script may use top-level `return`) | A syntax error stops the script |
| S02 | error | A password / token / secret assigned a literal (the value is never printed) | Readable by everyone with repository access |
| S03 | warning | `importPackage`, `importClass`, `JavaImporter`, `Packages.` | Nashorn/Rhino only; GraalJS (2304+) needs `Java.type()` |
| S04 | warning | A method **added after** the target release (or removed before it), from the SDK change log | Fails at runtime on an older server. Only reported when *every* class carrying that method name is newer, so names that also exist in an older class are not flagged |
| S05 | warning | Arithmetic on `getCustomField(...)` | A String: `'9' + 1 = '91'` |
| S06 | error | A compared string literal with a tab or leading/trailing space | The comparison can never match (dead branch) |
| S07 | info | `for (x in ...)` | Depends on the JS engine for Java collections |
| S08 | warning | `performAction()` with no check that the action was found | Silent no-op or TypeError |
| S09 | info | `save()` inside a hook with no guard | May re-trigger the hook |
| S10 | info | `switch` without `default` | An unexpected key becomes `undefined` / the text "undefined" |
| S11 | warning | A writer/stream opened but never `.close()`d | File-handle leak |
| S12 | error | A Save Hook (uses `PolarionScriptingException`) ending with a string | Any returned string counts as a failed script |
| S13 | warning | `eval(` / `new Function(` | Dynamic code |
| S14 | info | The same helper function defined in 3+ files | Fix every copy or use a script library |
| V01 | error | Velocity `#if/#foreach/#macro/#define` without `#end` or the reverse (comments ignored) | Breaks the template |
| V02 | warning | `getOldApi()` inside a `#foreach` | Slow on large projects |
| V03 | warning | A query term starting with `*` or `?` | Lucene rejects it |
| V04 | info | A `#set` variable never read (throw-away `$void = $list.add()` idioms are ignored) | Dead code |
| V05 | info | A macro call that is not built in, defined here or imported | Fails at render time |

## Verified on real files (2026-11-07)
- The 16 finished client scripts in `Documents\Claude Scripts` (target 2410): no syntax errors, no secrets, no API method newer than 2410. Only S14 (copy-pasted helpers, as the notes already say).
- The older reference scripts: S08 finds the unguarded `performAction` in `ApprovalScript.js` and `approval.js`, S09 the unguarded saves in `DocumentApproval.js` and `approval.js`: the issues the knowledge base lists as critical/high.
- All 202 templates and page script blocks of the real local Polarion 2606 demo projects: **no errors or warnings**. (The first version of V01 wrongly flagged 15 of them because of `#end"`; fixed and covered by a test.)

## Limits
- Name-level API checks: the receiver type of `x.method()` is unknown, so a method name that exists in an older class is never flagged, and a typo is not reported.
- Polarion Scripting scripts that live only in the server's database or file system (not the repository) cannot be read remotely; point the *Folder...* button at an export or your working copy.
- Not a replacement for running the script: it finds known patterns, not every bug.
