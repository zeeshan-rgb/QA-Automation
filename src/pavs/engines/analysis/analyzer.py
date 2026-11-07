"""Static analysis of Polarion scripts (JavaScript) and Velocity templates. Read-only, no AI, never executes anything.

Rules come from bugs documented in the user's own scripting notes plus the SDK change log. Every rule is a fixed
pattern; each finding names the file, the line and why it matters. The rule list is documented in
docs/SCRIPT-RULES.md - keep both in sync.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "api_methods.json"


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str          # error | warning | info
    kinds: tuple[str, ...]  # js | vm
    why: str


RULES: dict[str, Rule] = {r.id: r for r in [
    Rule("S01", "Script has no syntax errors", "error", ("js",), "A syntax error stops the script from running at all (checked with Node.js when available)."),
    Rule("S02", "No secrets written into scripts", "error", ("js", "vm"), "A password or token typed into a script is readable by everyone who can read the repository."),
    Rule("S03", "No Nashorn/Rhino-only constructs", "warning", ("js",), "importPackage/importClass/JavaImporter/Packages do not exist in GraalJS (Polarion 2304+); use Java.type()."),
    Rule("S04", "Only API methods that exist in the target release", "warning", ("js",), "A method added in a newer Polarion release fails at runtime on an older server."),
    Rule("S05", "No arithmetic on custom field values", "warning", ("js",), "getCustomField() often returns a String, so + concatenates ('9' + 1 = '91'); convert with Number()/parseInt()."),
    Rule("S06", "No stray whitespace in compared text", "error", ("js",), "A tab or space inside a compared string literal makes the comparison never match (dead branch)."),
    Rule("S07", "No for...in over Java collections", "info", ("js",), "for...in on a Java collection depends on the JS engine; use an index loop, for...of or forEach."),
    Rule("S08", "performAction is guarded", "warning", ("js",), "performAction(undefined) is a silent no-op or TypeError when the action is not available in the current state."),
    Rule("S09", "Hooks that save guard against re-triggering", "info", ("js",), "A Save Hook that calls save() on the object it was triggered by can re-run itself without an idempotency guard."),
    Rule("S10", "switch statements have a default", "info", ("js",), "Without default an unexpected key returns undefined, which can be written into a field as the text 'undefined'."),
    Rule("S11", "Opened files are closed", "warning", ("js",), "A BufferedWriter/FileWriter that is flushed but never closed leaks the file handle."),
    Rule("S12", "Save Hooks do not end with a string", "error", ("js",), "A Save Hook must end with true or nothing; ending with any string (even empty) counts as a failed script."),
    Rule("S13", "No eval / new Function", "warning", ("js",), "Dynamic code is a security and maintenance risk."),
    Rule("S14", "Helper functions are not copy-pasted", "info", ("js",), "The same helper defined in 3 or more files must be fixed in every copy; use a shared script library."),
    Rule("V01", "Velocity blocks are balanced", "error", ("vm",), "An #if/#foreach/#macro without #end (or the reverse) breaks the whole template."),
    Rule("V02", "No getOldApi() inside loops", "warning", ("vm",), "getOldApi() per item inside #foreach reads every object again; slow on large projects."),
    Rule("V03", "No leading wildcard in Lucene queries", "warning", ("vm",), "Polarion's query syntax does not allow a query term to start with * or ?."),
    Rule("V04", "No unused variables", "info", ("vm",), "A #set variable that is never read is dead code."),
    Rule("V05", "Macros are defined or imported", "info", ("vm",), "A call to a macro that is neither built in nor defined/imported here fails when rendered."),
]}


@dataclass
class SourceFile:
    name: str                  # display name, e.g. LockStatusEnforcement.js or widgets/cfd/render.vm
    kind: str                  # js | vm
    text: str
    origin: str = "local"      # local | repo


@dataclass
class Finding:
    rule: str
    file: str
    line: int
    message: str
    severity: str = ""

    def __post_init__(self) -> None:
        if not self.severity:
            self.severity = RULES[self.rule].severity

    def where(self) -> str:
        return f"{self.file}:{self.line}"


# ---------------------------------------------------------------- lexical helpers
def mask_js(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Code with comments and string contents blanked (newlines and length kept) + the string literals found."""
    out = list(text)
    strings: list[tuple[int, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and c != "`":
                    break
                j += 1
            strings.append((i, text[i + 1:j]))
            for k in range(i + 1, min(j, n)):
                if text[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out), strings


def mask_vm(text: str) -> str:
    """Velocity code without ## line comments and #* *# blocks."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"#\*.*?\*#", blank, text, flags=re.S)
    return re.sub(r"##[^\n]*", blank, text)


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


@lru_cache(maxsize=1)
def api_index() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {"since": {}, "removed": {}, "known": []}


def release_code(release: str | None) -> str | None:
    if not release:
        return None
    m = re.match(r"^(?:3\.)?(\d{2})\.?(\d{1,2})$", release.strip())
    return f"{m.group(1)}{int(m.group(2)):02d}" if m else None


# ---------------------------------------------------------------- JavaScript rules
SECRET = re.compile(r"""(?ix)\b(pass(?:word|wd)?|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*["'][^"'\s]{4,}["']""")
HOOK_MARK = re.compile(r"PolarionScriptingException")
JS_BUILTIN_NAMES = {"function", "if", "for", "while", "switch", "catch", "return", "typeof", "new"}


def find_node() -> str | None:
    import os
    for cand in (os.environ.get("PAVS_NODE"), shutil.which("node"), r"D:\nodejs\node.exe", r"C:\Program Files\nodejs\node.exe"):
        if cand and Path(cand).exists():
            return cand
    return None


def syntax_check(text: str, node: str | None) -> list[tuple[int, str]]:
    if not node:
        return []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "check.js"
        p.write_text("(function(){\n" + text + "\n})", encoding="utf-8")
        try:
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return []
    if r.returncode == 0:
        return []
    err = r.stderr or ""
    m = re.search(r"check\.js:(\d+)", err)
    line = max(1, int(m.group(1)) - 1) if m else 1
    msg = next((l.strip() for l in err.splitlines() if "Error" in l), "syntax error")
    return [(line, msg)]


def analyze_js(f: SourceFile, target: str | None, node: str | None, check_syntax: bool = True) -> list[Finding]:
    out: list[Finding] = []
    code, strings = mask_js(f.text)

    def add(rule: str, idx: int, msg: str) -> None:
        out.append(Finding(rule, f.name, line_of(f.text, idx), msg))

    if check_syntax:
        for line, msg in syntax_check(f.text, node):
            out.append(Finding("S01", f.name, line, msg))
    for m in SECRET.finditer(re.sub(r"//[^\n]*", "", f.text)):
        add("S02", m.start(), f"'{m.group(1)}' is assigned a literal value (value not shown)")
    for m in re.finditer(r"\b(importPackage|importClass|JavaImporter)\s*\(|\bPackages\.", code):
        add("S03", m.start(), f"'{m.group(0).strip('(')}' is Nashorn/Rhino only; use Java.type('...')")
    idx = api_index()
    tgt = release_code(target)
    seen: set[str] = set()
    for m in re.finditer(r"\.\s*([A-Za-z_]\w*)\s*\(", code):
        name = m.group(1)
        if name in seen:
            continue
        since, gone = idx["since"].get(name), idx["removed"].get(name)
        if tgt and since and since > tgt:
            seen.add(name)
            add("S04", m.start(), f".{name}() was added in Polarion {since}; the target release is {tgt}")
        elif tgt and gone and gone <= tgt:
            seen.add(name)
            add("S04", m.start(), f".{name}() was removed in Polarion {gone}; the target release is {tgt}")
    for m in re.finditer(r"\bgetCustomField(?:Value)?\s*\([^)]*\)\s*[+\-*/]\s*\d|\b\w*[vV]ersion\w*\s*\+\s*\d", code):
        add("S05", m.start(), "arithmetic on a custom field value: it may be a String ('9' + 1 = '91')")
    for pos, lit in strings:
        if re.search(r"(?:===?|!==?)\s*$", code[max(0, pos - 6):pos]) or re.search(r"^\s*(?:===?|!==?)", code[pos + len(lit) + 2:pos + len(lit) + 8]):
            if lit and (lit != lit.strip() or "\t" in lit or "\\t" in lit):   # a real tab or an escaped \t
                add("S06", pos, f"compared text {lit!r} contains leading/trailing whitespace or a tab, so it can never match a real value")
    for m in re.finditer(r"\bfor\s*\(\s*(?:var|let|const)?\s*\w+\s+in\s+", code):
        add("S07", m.start(), "for...in over what may be a Java collection")
    if "performAction" in code:
        for m in re.finditer(r"\.performAction\s*\(", code):
            window = code[max(0, m.start() - 900):m.start()]
            if not re.search(r"(?:undefined|null|===?\s*(?:undefined|null)|!==?\s*(?:undefined|null)|if\s*\(\s*!?\s*\w*[aA]ction\w*\s*\)|\.length|isEmpty)", window):
                add("S08", m.start(), "performAction() without a check that the action was found")
                break
    hook = bool(HOOK_MARK.search(code)) or bool(re.search(r"\bcontext\s*\.\s*getTarget\s*\(", code))
    if hook and re.search(r"\.save\s*\(\s*\)", code) and not re.search(r"(?:recurs|guard|already|isModified|alreadyDone|skip)", f.text, re.I):
        m = re.search(r"\.save\s*\(\s*\)", code)
        add("S09", m.start(), "save() inside a hook without an idempotency guard")
    for m in re.finditer(r"\bswitch\s*\(", code):
        depth, j, body_start = 0, m.end(), None
        while j < len(code):
            if code[j] == "{":
                depth += 1
                body_start = body_start or j
            elif code[j] == "}":
                depth -= 1
                if depth == 0 and body_start:
                    break
            j += 1
        if body_start and not re.search(r"\bdefault\s*:", code[body_start:j]):
            add("S10", m.start(), "switch without a default case")
    for m in re.finditer(r"\bnew\s+(?:java\.io\.)?(BufferedWriter|FileWriter|FileOutputStream|PrintWriter)\b", code):
        if not re.search(r"\.close\s*\(", code):
            add("S11", m.start(), f"{m.group(1)} is opened but .close() is never called")
            break
    if HOOK_MARK.search(code):
        tail = code.rstrip().rstrip(";").rstrip()
        last = strings[-1] if strings else None
        if last and f.text.rstrip().rstrip(";").rstrip().endswith(("'", '"')) and re.search(r"[\n;]\s*[\"'][^\n]*[\"']\s*;?\s*$", f.text.rstrip()):
            add("S12", len(f.text.rstrip()) - 1, "the script ends with a string literal; a Save Hook must end with true or nothing")
    for m in re.finditer(r"\beval\s*\(|\bnew\s+Function\s*\(", code):
        add("S13", m.start(), "dynamic code execution")
    return out


# ---------------------------------------------------------------- Velocity rules
DIRECTIVES = {"if", "elseif", "else", "end", "set", "foreach", "macro", "include", "parse", "stop", "break", "evaluate",
              "define", "literal", "import", "info", "warning", "error", "message", "loadcss", "loadjs", "loadwidgetcss",
              "loadwidgetjs", "documentpanel", "foreach", "return", "{else}", "{end}"}
OPENERS = ("if", "foreach", "macro", "define")


def analyze_vm(f: SourceFile) -> list[Finding]:
    out: list[Finding] = []
    code = mask_vm(f.text)

    def add(rule: str, idx: int, msg: str) -> None:
        out.append(Finding(rule, f.name, line_of(f.text, idx), msg))

    for m in SECRET.finditer(re.sub(r"##[^\n]*", "", f.text)):
        add("S02", m.start(), f"'{m.group(1)}' is assigned a literal value (value not shown)")
    stack: list[tuple[str, int]] = []
    for m in re.finditer(r"#\{?(if|foreach|macro|define)\}?(?=\s*\()|#\{?(end)\}?(?!\w)", code):
        kind = m.group(1) or m.group(2)
        if kind == "end":
            if not stack:
                add("V01", m.start(), "#end without an opening #if/#foreach/#macro")
            else:
                stack.pop()
        else:
            stack.append((kind, m.start()))
    for kind, pos in stack:
        add("V01", pos, f"#{kind} is never closed with #end")
    for m in re.finditer(r"#foreach\b", code):
        body = code[m.end():m.end() + 2500]
        e = re.search(r"#\{?end\}?", body)
        if re.search(r"getOldApi\s*\(", body[:e.start() if e else len(body)]):
            add("V02", m.start(), "getOldApi() is called inside a #foreach loop")
    for m in re.finditer(r"""["'][^"'\n]*(?:^|[\s(:\"'])[*?][A-Za-z0-9_][^"'\n]*["']""", f.text):
        s = m.group(0)
        if re.search(r"\b(?:type|status|title|id|author|description|project\.id|[a-zA-Z]+):\s*[*?]\w", s):
            add("V03", m.start(), "a query term starts with a wildcard (*/?), which Lucene rejects")
    defined = {m.group(1).lower() for m in re.finditer(r"#macro\s*\(\s*(\w+)", code)}
    for m in re.finditer(r"#set\s*\(\s*\$!?\{?(\w+)\}?\s*=([^\n]*)", code):
        name, rhs = m.group(1), m.group(2)
        if re.search(r"\.(?:add|put|remove|addAll|putAll|clear|append)\s*\(", rhs) or name.lower() in ("void", "dummy", "ignore", "unused"):
            continue          # throw-away variable: the call is made for its side effect
        if len(re.findall(r"\$!?\{?" + re.escape(name) + r"\b", code)) <= 1:
            add("V04", m.start(), f"${name} is set but never used")
    imports_macros = bool(re.search(r"#import\s*\(", code)) or bool(re.search(r"#parse\s*\(", code))
    for m in re.finditer(r"#(\w+)\s*\(", code):
        name = m.group(1).lower()
        if name not in DIRECTIVES and name not in defined and not imports_macros:
            add("V05", m.start(), f"macro #{m.group(1)}() is not defined here and nothing is imported")
            break
    return out


# ---------------------------------------------------------------- whole-set analysis
def analyze_files(files: list[SourceFile], target_release: str | None = None, check_syntax: bool = True
                  ) -> tuple[list[Finding], list[str]]:
    """(findings, notes)."""
    node = find_node() if check_syntax else None
    notes = []
    if check_syntax and not node and any(f.kind == "js" for f in files):
        notes.append("Node.js was not found, so JavaScript syntax (S01) was not checked.")
    findings: list[Finding] = []
    helpers: dict[str, set[str]] = {}
    for f in files:
        if f.kind == "js":
            findings += analyze_js(f, target_release, node, check_syntax)
            code, _ = mask_js(f.text)
            for m in re.finditer(r"\bfunction\s+(\w+)\s*\(", code):
                helpers.setdefault(m.group(1), set()).add(f.name)
        elif f.kind == "vm":
            findings += analyze_vm(f)
    for name, where in sorted(helpers.items()):
        if len(where) >= 3 and name not in ("main", "run"):
            findings.append(Finding("S14", sorted(where)[0], 1, f"function {name}() is defined in {len(where)} files: " + ", ".join(sorted(where)[:6])))
    findings.sort(key=lambda x: ({"error": 0, "warning": 1, "info": 2}[x.severity], x.file, x.line))
    return findings, notes
