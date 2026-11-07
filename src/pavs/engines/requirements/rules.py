"""Rule-based requirement -> check generation. No AI, no network: fixed, documented sentence patterns.

A sentence that no rule understands becomes a "not automatable" finding. It is never guessed.
The patterns are documented in docs/REQUIREMENT-PATTERNS.md; keep both in sync.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...domain.enums import TestOrigin
from ...domain.requirements import (CheckKind, CheckSpec, Expectation, Finding, FindingKind, Polarity,
                                    Requirement, TestCase)

# ---------------------------------------------------------------- vocabulary (edit here, then the doc)
OBJECT_WORDS = [  # (regex, permission object, label)
    (r"comments?", "Comment", "comments"),
    (r"documents?|modules?|live\s*docs?|specifications?", "Module", "documents"),
    (r"test\s*runs?", "TestRun", "test runs"),
    (r"pages?|reports?", "RichPage", "pages"),
    (r"plans?", "Plan", "plans"),
    (r"baselines?", "Baseline", "baselines"),
    (r"work\s*items?|requirements?|tasks?|defects?|test\s*cases?|items?|issues?", "WorkItem", "Work Items"),
]
VERBS = {  # word -> action suffix
    "read": "read", "view": "read", "see": "read", "open": "read", "access": "read",
    "modify": "modify", "edit": "modify", "change": "modify", "update": "modify",
    "delete": "delete", "remove": "delete",
    "create": "create", "add": "create",
}
REVIEW_WORDS = {"review"}    # documented mapping: review = read + comment (Polarion has no 'review' permission)
UNMAPPED_VERBS = {"approve", "reject", "export", "import", "link", "assign", "watch", "sign", "publish", "baseline"}
PERM_PREFIX = "com.polarion.persistence.object."

NEG = (r"(?:cannot|can\s*not|can't|must\s+not|should\s+not|shall\s+not|may\s+not|mustn't|shouldn't|shan't|"
       r"(?:is|are)\s+not\s+(?:allowed|able)\s+to|not\s+be\s+(?:allowed|able)\s+to|"
       r"(?:must|should|shall|may)\s+not\s+(?:have\s+(?:the\s+)?permissions?\s+to|be\s+(?:allowed|able)\s+to)|"
       r"(?:has|have)\s+no\s+permissions?\s+to|no\s+permissions?\s+to|without\s+permissions?\s+to)")
POS = (r"(?:(?:can|may|should|must|shall|will)(?:\s+be\s+(?:able|allowed)\s+to|\s+have\s+(?:the\s+)?permissions?\s+to)?|"
       r"(?:is|are)\s+(?:allowed|able)\s+to|(?:has|have)\s+(?:the\s+)?permissions?\s+to|"
       r"with\s+(?:the\s+)?permissions?\s+to|permissions?\s+to)")
NEG_RE = re.compile(NEG, re.I)
POS_RE = re.compile(POS, re.I)
NAME = r"""["“'‘]?(?P<{g}>[A-Za-z0-9][\w\-]*(?:[ ][A-Za-z0-9][\w\-]*){{0,5}}?)["”'’]?"""
PRONOUN = re.compile(r"^(?:it|they|them|he|she|(?:the|this|that)?\s*(?:role|group)|users|the users)$", re.I)
STOP_TAIL = r"(?=\s*(?:[.,;:]|$)|\s+(?:should|must|shall|can|may|cannot|will|has|have|with|that|which|is|are|to|in|for|on|and|but|when|if|so|by|of|as|type|called|named|from|exists?|at|under|within)\b)"
UNQ = r"""(?P<{g}>[A-Za-z0-9][\w\-]*(?:[ ][A-Za-z0-9][\w\-]*){{0,5}}?)"""


def _q(group: str) -> str:
    return rf"""["“'‘]?{UNQ.format(g=group)}["”'’]?"""


@dataclass
class Names:
    """Optional hints from an inspected configuration (display names and ids)."""
    roles: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    @classmethod
    def from_view(cls, view) -> "Names":
        roles = [r["id"] for r in view.roles()]
        types = [x for t in view.types() for x in (t["id"], t.get("name") or "")]
        fields = [x for f in view.items("fields", "layers") + view.items("fields", "effective")
                  for x in (f["id"], f.get("name") or "")]
        return cls(sorted(set(roles)), sorted(set(x for x in types if x)), sorted(set(x for x in fields if x)))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _known_in(sentence: str, names: list[str]) -> str | None:
    """Longest known name (any spelling) that occurs in the sentence."""
    ns = _norm(sentence)
    best = None
    for n in sorted(names, key=len, reverse=True):
        if len(_norm(n)) >= 3 and _norm(n) in ns and (best is None):
            # require a word boundary-ish match on the original text to avoid 'task' in 'multitasking'
            pat = r"\b" + r"[\s_\-]*".join(re.escape(p) for p in re.split(r"[\s_\-]+", n.strip()) if p) + r"s?\b"
            if re.search(pat, sentence, re.I):
                best = n
    return best


def _clean_name(s: str) -> str:
    s = s.strip(" \t\"“”'‘’.,;:")
    s = re.sub(r"^(?:the|a|an|new|polarion|custom|work\s*item|project|global)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+(?:role|field|type|enumeration|status|group|in the project|in project)$", "", s, flags=re.I)
    return s.strip()


@dataclass
class Ctx:
    role: str | None = None
    wi_type: str | None = None
    obj: str = "WorkItem"
    obj_label: str = "Work Items"


@dataclass
class Hit:
    check: CheckSpec
    title: str
    rule: str
    polarity: Polarity = Polarity.POSITIVE
    note: str = ""


# ---------------------------------------------------------------- helpers
def _type_in(sentence: str, names: Names, ctx: Ctx) -> str | None:
    t = _known_in(sentence, names.types)
    if t:
        return t
    m = re.search(rf"(?:work\s*items?\s+of\s+type|items?\s+of\s+type|(?:for|in|on)\s+(?:the\s+)?type)\s+{_q('t')}{STOP_TAIL}", sentence, re.I)
    if m:
        return _clean_name(m.group("t"))
    m = re.search(rf"\bfor\s+(?:all\s+|every\s+|the\s+)?{_q('t')}\s+(?:work\s*items?|items?)\b", sentence, re.I)
    if m:
        return _clean_name(m.group("t"))
    m = re.search(rf"\b(?:all|every|each|the)?\s*([A-Z][\w\-]*(?:\s[A-Z][\w\-]*)?)\s+work\s*items?\b", sentence)
    if m:
        return _clean_name(m.group(1))
    return ctx.wi_type


def _object_in(clause: str, ctx: Ctx) -> tuple[str, str]:
    for rx, perm_obj, label in OBJECT_WORDS:
        if re.search(rf"\b(?:{rx})\b", clause, re.I):
            return perm_obj, label
    if re.search(r"\b(them|it|those)\b", clause, re.I):
        return ctx.obj, ctx.obj_label
    return ctx.obj, ctx.obj_label


def _split_list(s: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|\band\b|\bor\b|&|/)\s*", s)
    return [_clean_name(p) for p in parts if _clean_name(p)]


# ---------------------------------------------------------------- rules
def rule_permission(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    neg = NEG_RE.search(sentence)
    pos = None if neg else POS_RE.search(sentence)
    m = neg or pos
    if not m:
        return None
    tail = sentence[m.end():]
    words = re.findall(r"[a-z']+", tail.lower())
    verbs = [w for w in words if w in VERBS or w in REVIEW_WORDS or w in UNMAPPED_VERBS or w == "comment"]
    if not verbs:
        return None
    # status-transition sentences are handled by rule_transition
    if re.search(r"\bfrom\b.+\bto\b", tail, re.I) and re.search(r"\b(move|transition|go|be\s+set|change)\b", tail, re.I):
        return None
    subject = sentence[:m.start()].strip()
    role = _role_from_subject(subject, names, ctx)
    if not role:
        return None
    ctx.role = role
    perm_obj, label = _object_in(tail, ctx)
    ctx.obj, ctx.obj_label = perm_obj, label
    expect = Expectation.DENIED if neg else Expectation.GRANTED
    pol = Polarity.NEGATIVE if neg else Polarity.POSITIVE
    hits: list[Hit] = []
    for v in dict.fromkeys(verbs):
        if v in UNMAPPED_VERBS:
            continue
        if v in REVIEW_WORDS:
            for obj, suf in (("WorkItem", "read"), ("Comment", "create")):
                hits.append(Hit(CheckSpec(kind=CheckKind.PERMISSION, expect=expect,
                                          target={"role": role, "permission": f"{PERM_PREFIX}{obj}.{suf}"},
                                          note="'review' is checked as read + comment (Polarion has no review permission)"),
                                f"Role '{role}' {'cannot' if neg else 'can'} review (= {obj}.{suf})", "permission/review", pol,
                                "documented mapping: review = WorkItem.read + Comment.create"))
            continue
        obj = "Comment" if (v == "comment" or perm_obj == "Comment") else perm_obj
        suf = VERBS.get(v, "create" if v == "comment" else None)
        if v == "comment":
            obj, suf = "Comment", "create"
        if suf is None:
            continue
        hits.append(Hit(CheckSpec(kind=CheckKind.PERMISSION, expect=expect,
                                  target={"role": role, "permission": f"{PERM_PREFIX}{obj}.{suf}"}),
                        f"Role '{role}' {'cannot' if neg else 'can'} {v} {label}", "permission", pol))
    if "comment" in words and not any("Comment" in h.check.target.get("permission", "") for h in hits):
        hits.append(Hit(CheckSpec(kind=CheckKind.PERMISSION, expect=expect,
                                  target={"role": role, "permission": f"{PERM_PREFIX}Comment.create"}),
                        f"Role '{role}' {'cannot' if neg else 'can'} comment", "permission/comment", pol))
    unmapped = [v for v in dict.fromkeys(verbs) if v in UNMAPPED_VERBS]
    if unmapped and not hits:
        return None
    return hits or None


def _role_from_subject(subject: str, names: Names, ctx: Ctx) -> str | None:
    subject = re.sub(r"^(?:and|but|also|then|so|that|which|where|when|if)(?:\s+|$)", "", subject.strip(), flags=re.I).strip()
    if not subject or PRONOUN.match(subject) or PRONOUN.match(_clean_name(subject) or subject):
        return ctx.role
    known = _known_in(subject, names.roles)
    if known:
        return known
    m = re.search(rf"{_q('r')}\s+role\b", subject, re.I) or re.search(rf"\brole\s+(?:called|named)?\s*{_q('r')}\s*$", subject, re.I)
    if m:
        return _clean_name(m.group("r"))
    cleaned = _clean_name(re.sub(r"^(?:users?|people|members?|everyone)\s+(?:with|in|having|of)\s+(?:the\s+)?", "", subject, flags=re.I))
    if cleaned and len(cleaned.split()) <= 5 and not re.match(r"^(?:there|this|that|the system|polarion|a user|any user)$", cleaned, re.I):
        return cleaned
    return None


def rule_role_exists(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b(?:create|add|define|set\s*up|configure|provide|introduce|have|exist)\b[^.]*?\brole\s+(?:called|named)?\s*{_q('r')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\brole\s+(?:called|named)\s*{_q('r')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\b(?:there\s+(?:is|should\s+be|must\s+be))\s+(?:a|an)?\s*{_q('r')}\s+role\b", sentence, re.I) \
        or re.search(rf"\b(?:the\s+)?{_q('r')}\s+role\s+(?:should|must|shall)\s+exist\b", sentence, re.I) \
        or re.search(rf"\brole\s+(?:called|named)?\s*{_q('r')}\s+(?:exists?|is\s+(?:defined|available|present|configured|created))\b", sentence, re.I)
    if not m:
        return None
    exists_verb = re.search(r"\b(create|add|define|set\s*up|configure|provide|introduce|exists?|defined|available|present|configured|created|there\s+(?:is|should|must))\b", sentence, re.I)
    if not exists_verb:
        return None
    role = _clean_name(m.group("r"))
    ctx.role = role
    return [Hit(CheckSpec(kind=CheckKind.ROLE_EXISTS, expect=Expectation.PRESENT, target={"role": role}),
                f"Role '{role}' exists", "role_exists")]


def rule_role_assigned(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\buser\s+{_q('u')}\s+(?:should|must|shall)?\s*(?:has|have|is\s+assigned|gets?|holds?)\s+(?:the\s+)?(?:role\s+)?{_q('r')}(?:\s+role)?{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\bassign\s+(?:the\s+)?(?:role\s+)?{_q('r')}(?:\s+role)?\s+to\s+(?:user\s+)?{_q('u')}{STOP_TAIL}", sentence, re.I)
    if not m:
        return None
    return [Hit(CheckSpec(kind=CheckKind.ROLE_ASSIGNED, expect=Expectation.PRESENT,
                          target={"role": _clean_name(m.group("r")), "user": _clean_name(m.group("u"))}),
                f"User '{_clean_name(m.group('u'))}' has role '{_clean_name(m.group('r'))}'", "role_assigned")]


def rule_field_required(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    st = r"(?P<m>mandatory|required|optional|read-?only|not\s+(?:mandatory|required))"
    m = re.search(rf"\b(?:field|property|attribute)\s+{_q('f')}\s+(?:is|are|must\s+be|should\s+be|shall\s+be|has\s+to\s+be|needs?\s+to\s+be|becomes?)\s+{st}", sentence, re.I) \
        or re.search(rf"\bmake\s+(?:the\s+)?(?:field\s+)?{_q('f')}\s+(?:field\s+)?{st}", sentence, re.I)
    known = _known_in(sentence, names.fields)
    if not m and known:
        m2 = re.search(rf"{st}", sentence, re.I)
        if m2 and re.search(r"\b(is|are|must\s+be|should\s+be|shall\s+be|make)\b", sentence, re.I):
            fld, mode = known, m2.group("m")
        else:
            return None
    elif m:
        fld, mode = _clean_name(m.group("f")), m.group("m")
        if known and _norm(known) in _norm(sentence):
            fld = known
    else:
        return None
    if re.search(r"\b(when|before|upon)\b.*\b(action|transition|review|approv|reject|start|complet|clos|submit|releas|publish)", sentence, re.I):
        return None      # handled by rule_action_field
    wi = _type_in(sentence, names, ctx)
    ctx.wi_type = wi or ctx.wi_type
    mode_l = mode.lower().replace("-", "")
    if mode_l.startswith("readonly"):
        return [Hit(CheckSpec(kind=CheckKind.FIELD_READ_ONLY, expect=Expectation.PRESENT, target={"field": fld, "type": wi}),
                    f"Field '{fld}' is read-only" + (f" for {wi}" if wi else ""), "field_read_only")]
    exp = Expectation.OPTIONAL if mode_l.startswith(("optional", "not")) else Expectation.REQUIRED
    return [Hit(CheckSpec(kind=CheckKind.FIELD_REQUIRED, expect=exp, target={"field": fld, "type": wi}),
                f"Field '{fld}' is {'optional' if exp == Expectation.OPTIONAL else 'mandatory'}" + (f" for {wi}" if wi else ""),
                "field_required")]


def rule_missing_field_rejected(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b(?:saving|creating|submitting|save|create|submit)\s+(?:a\s+|an\s+|the\s+)?(?:(?P<t>[A-Z][\w\-]*(?:\s[A-Z][\w\-]*)?)\s+)?(?:work\s*items?\s+)?without\s+(?:a\s+|the\s+|an\s+)?(?:value\s+(?:for|in)\s+)?(?:field\s+)?{_q('f')}\s+(?:is|are|must\s+be|should\s+be|shall\s+be|fails?|gets?)?\s*(?:rejected|blocked|refused|not\s+allowed|prevented|fails?|impossible|an?\s+error)", sentence, re.I)
    if not m:
        return None
    fld = _clean_name(m.group("f"))
    wi = (_clean_name(m.group("t")) if m.group("t") else None) or ctx.wi_type
    ctx.wi_type = wi
    return [Hit(CheckSpec(kind=CheckKind.FIELD_REQUIRED, expect=Expectation.REQUIRED, target={"field": fld, "type": wi}),
                f"Field '{fld}' is mandatory" + (f" for {wi}" if wi else ""), "field_required (restated as rejection)")]


def rule_action_field(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b{_q('a')}\s+(?:action|transition)\s+(?:requires|needs|must\s+have|should\s+require|shall\s+require)\s+(?:the\s+)?(?:field\s+)?{_q('f')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\b(?:field\s+)?{_q('f')}\s+(?:is|must\s+be|should\s+be)\s+(?:required|mandatory)\s+(?:when|to|before|upon)\s+(?:the\s+)?(?P<a>(?:review|approv|reject|start|complet|clos|submit|releas|publish|resolv|verif|finish|send|rework|accept)\w*)(?:\s+action|\s+transition)?{STOP_TAIL}", sentence, re.I)
    if not m:
        return None
    act, fld = _clean_name(m.group("a")), _clean_name(m.group("f"))
    wi = _type_in(sentence, names, ctx)
    ctx.wi_type = wi or ctx.wi_type
    return [Hit(CheckSpec(kind=CheckKind.ACTION_REQUIRES_FIELD, expect=Expectation.REQUIRED,
                          target={"action": act, "field": fld, "type": wi}),
                f"Action '{act}' requires field '{fld}'" + (f" ({wi})" if wi else ""), "action_requires_field")]


def rule_transition(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\bfrom\s+(?:status\s+|state\s+)?{_q('a')}\s+to\s+(?:status\s+|state\s+)?{_q('b')}{STOP_TAIL}", sentence, re.I)
    if not m:
        return None
    verb = re.search(r"\b(move|moved|moving|transition|go|goes|change|set|switch|promote)\b", sentence, re.I)
    if not verb:
        return None
    neg = NEG_RE.search(sentence)
    a, b = _clean_name(m.group("a")), _clean_name(m.group("b"))
    wi = _type_in(sentence[:m.start()], names, ctx)
    ctx.wi_type = wi or ctx.wi_type
    exp = Expectation.BLOCKED if neg else Expectation.ALLOWED
    return [Hit(CheckSpec(kind=CheckKind.TRANSITION, expect=exp, target={"type": wi, "from_status": a, "to_status": b}),
                f"{wi or 'Work Items'} {'cannot' if neg else 'can'} move from {a} to {b}", "transition",
                Polarity.NEGATIVE if neg else Polarity.POSITIVE)]


def rule_enum_contains(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b(?:the\s+)?{_q('e')}\s+(?:enumeration|dropdown|drop-down|picklist|pick\s+list|list|field|values)\s+(?P<neg>(?:should|must|shall|has\s+to)\s+not|(?:should|must|shall|has\s+to)|(?:contains?|includes?|offers?))\s*(?:contain|include|have|offer|provide)?\s*(?:the\s+)?(?:following\s+)?(?:values?|options?|entries)?\s*:?\s*(?P<vals>.+)$", sentence, re.I)
    if not m or not re.search(r"\b(contain|include|have|offer|provide|contains|includes|offers)\b", sentence, re.I):
        return None
    vals = _split_list(m.group("vals"))
    if not vals:
        return None
    enum = _clean_name(m.group("e"))
    neg = bool(re.search(r"\bnot\b", m.group("neg"), re.I))
    wi = _type_in(sentence, names, ctx)
    return [Hit(CheckSpec(kind=CheckKind.ENUM_CONTAINS, expect=Expectation.ABSENT if neg else Expectation.PRESENT,
                          target={"enum": enum, "options": vals, "type": wi}),
                f"'{enum}' {'does not contain' if neg else 'contains'} {', '.join(vals)}", "enum_contains",
                Polarity.NEGATIVE if neg else Polarity.POSITIVE)]


def rule_status_exists(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b(?:have|has|include|includes|add|create|define|offer|provide|exist)s?\b[^.]*?\b(?:status|state)\s+(?:called|named)?\s*{_q('s')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\b(?:status|state)\s+{_q('s')}\s+(?:should|must|shall)\s+exist", sentence, re.I) \
        or re.search(rf"\bstatus\s+(?:is|becomes|changes\s+to|should\s+be)\s+{_q('s')}{STOP_TAIL}", sentence, re.I)
    if not m:
        return None
    s = _clean_name(m.group("s"))
    wi = _type_in(sentence, names, ctx)
    ctx.wi_type = wi or ctx.wi_type
    return [Hit(CheckSpec(kind=CheckKind.STATUS_EXISTS, expect=Expectation.PRESENT, target={"status": s, "type": wi}),
                f"Status '{s}' exists" + (f" for {wi}" if wi else ""), "status_exists")]


def rule_link_role(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\blink\s+role\s+(?:called|named)?\s*{_q('l')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\b(?:link(?:ed)?|trac(?:e|ed))\s+(?:to|with)\b.*?\b(?:using|via|with|by)\s+(?:the\s+)?(?:link\s+)?(?:role\s+)?{_q('l')}\s+(?:link|role)", sentence, re.I)
    if not m:
        return None
    lr = _clean_name(m.group("l"))
    return [Hit(CheckSpec(kind=CheckKind.LINK_ROLE_EXISTS, expect=Expectation.PRESENT, target={"link_role": lr}),
                f"Link role '{lr}' exists", "link_role_exists")]


def rule_document_type(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\bdocument\s+type\s+(?:called|named)?\s*{_q('d')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\b(?:create|add|define|have)\b[^.]*?\b{_q('d')}\s+document\s+type\b", sentence, re.I)
    if not m:
        return None
    d = _clean_name(m.group("d"))
    return [Hit(CheckSpec(kind=CheckKind.DOCUMENT_TYPE_EXISTS, expect=Expectation.PRESENT, target={"document_type": d}),
                f"Document type '{d}' exists", "document_type_exists")]


def rule_type_exists(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    if re.search(r"\bfield\b", sentence, re.I):
        return None
    m = re.search(rf"\b(?:create|add|define|have|provide|exist)\b[^.]*?\b(?:work\s*item\s+type\s+(?:called|named)?|type\s+(?:called|named))\s*{_q('t')}{STOP_TAIL}", sentence, re.I) \
        or re.search(rf"\b(?:create|add|define)\s+(?:a\s+|an\s+)?(?:new\s+)?{_q('t')}\s+work\s*item\s+type\b", sentence, re.I) \
        or re.search(rf"\bwork\s*item\s+type\s+{_q('t')}\s+(?:should|must|shall)\s+exist", sentence, re.I)
    if not m:
        return None
    t = _clean_name(m.group("t"))
    ctx.wi_type = t
    return [Hit(CheckSpec(kind=CheckKind.TYPE_EXISTS, expect=Expectation.PRESENT, target={"type": t}),
                f"Work Item type '{t}' exists", "type_exists")]


def rule_field_exists(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b(?:create|add|define|have|provide|exist|need)s?\b[^.]*?\b(?:custom\s+)?field\s+(?:called|named)?\s*{_q('f')}(?=\s*(?:[.,;:]|$)|\s+(?:of|as|type|for|in|on|with|that|which|to|should|must|shall)\b)(?P<rest>.*)$", sentence, re.I) \
        or re.search(rf"\b(?:custom\s+)?field\s+{_q('f')}\s+(?:should|must|shall)\s+exist(?P<rest>.*)$", sentence, re.I)
    if not m:
        return None
    fld = _clean_name(m.group("f"))
    rest = m.group("rest") or ""
    ft = re.search(r"\b(?:of\s+type|as\s+(?:a\s+)?|type)\s*(string|text|rich\s*text|integer|number|float|boolean|checkbox|date|date-?time|enum\w*|dropdown|duration)", rest, re.I)
    wi = _type_in(re.sub(r"\bof\s+type\s+\w+|\btype\s+(?:string|text|integer|number|float|boolean|date|enum\w*|duration)\b", " ", sentence, flags=re.I), names, ctx)
    ctx.wi_type = wi or ctx.wi_type
    return [Hit(CheckSpec(kind=CheckKind.FIELD_EXISTS, expect=Expectation.PRESENT,
                          target={"field": fld, "type": wi, "field_type": ft.group(1).lower() if ft else None}),
                f"Field '{fld}' exists" + (f" for {wi}" if wi else ""), "field_exists")]


def rule_traceability(sentence: str, ctx: Ctx, names: Names) -> list[Hit] | None:
    m = re.search(rf"\b(?:every|each|all)\s+{_q('a')}s?\s+(?:must|should|shall|has\s+to|have\s+to)\s+be\s+(?:linked|traced|covered|verified|connected)\s+(?:to|by|with)\s+(?:at\s+least\s+one\s+|a\s+|an\s+)?{_q('b')}s?{STOP_TAIL}", sentence, re.I)
    if not m:
        return None
    a, b = _clean_name(m.group("a")), _clean_name(m.group("b"))
    a = re.sub(r"s$", "", a) if len(a) > 4 else a
    return [Hit(CheckSpec(kind=CheckKind.TRACEABILITY, expect=Expectation.COVERED, target={"from_type": a, "to_type": b},
                          note="checked as: the configuration allows such a link; coverage of existing data is not measured"),
                f"Every {a} is linked to a {b}", "traceability")]


RULES = [rule_transition, rule_action_field, rule_missing_field_rejected, rule_field_required, rule_enum_contains, rule_role_assigned,
         rule_permission, rule_role_exists, rule_document_type, rule_link_role, rule_field_exists,
         rule_type_exists, rule_status_exists, rule_traceability]


# ---------------------------------------------------------------- sentence handling
def split_sentences(text: str) -> list[str]:
    out = []
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“'])|\n+", text):
        for part in re.split(r"\s*;\s*|\s*,\s*(?=but\b)|\s+(?=but\b)", s):
            if part.strip(" .,;"):
                out.append(part.strip())
    return out


VAGUE = re.compile(r"\b(fast|quick(?:ly)?|easy|easily|user[- ]friendly|intuitive|appropriate|adequate|reasonable|"
                   r"as needed|if possible|etc\.?|and/or|robust|flexible|efficient(?:ly)?|good|better|properly)\b", re.I)
CONFLICT_PAIRS = [(Expectation.GRANTED, Expectation.DENIED), (Expectation.REQUIRED, Expectation.OPTIONAL),
                  (Expectation.ALLOWED, Expectation.BLOCKED), (Expectation.PRESENT, Expectation.ABSENT)]


@dataclass
class GenerationResult:
    cases: list[TestCase]
    findings: list[Finding]


def generate_for_requirement(req: Requirement, names: Names | None = None) -> GenerationResult:
    names = names or Names()
    ctx = Ctx()
    cases: list[TestCase] = []
    findings: list[Finding] = []
    seen: dict[str, TestCase] = {}

    sources = [("", s) for s in split_sentences(req.text)] + \
              [(c, s) for c in req.acceptance_criteria for s in (split_sentences(c) or [c])]
    unresolved: list[str] = []
    for criterion, sentence in sources:
        if len(sentence.split()) < 3:
            continue
        hits = None
        for rule in RULES:
            hits = rule(sentence, ctx, names)
            if hits:
                break
        if not hits:
            if VAGUE.search(sentence):
                continue            # reported as "ambiguous" below
            if criterion or not _is_context_sentence(sentence):
                unresolved.append(sentence)
            continue
        for h in hits:
            key = h.check.model_dump_json(exclude={"note"})
            if key in seen:
                if criterion and not seen[key].criterion:
                    seen[key].criterion = criterion
                continue
            tc = TestCase(requirement_id=req.id, criterion=criterion or sentence, title=h.title, polarity=h.polarity,
                          check=h.check, origin=TestOrigin.RULE_GENERATED, rationale=f"rule: {h.rule}"
                          + (f" ({h.note})" if h.note else ""))
            seen[key] = tc
            cases.append(tc)

    for s in unresolved:
        findings.append(Finding(requirement_id=req.id, kind=FindingKind.NOT_AUTOMATABLE,
                                message=f"No rule understands this sentence, so no test was generated: \"{s[:160]}\""))
    if not req.acceptance_criteria:
        findings.append(Finding(requirement_id=req.id, kind=FindingKind.MISSING_CRITERIA,
                                message="No acceptance criteria were stated; tests were derived from the requirement text only."))
    for m in VAGUE.finditer(req.text + " " + " ".join(req.acceptance_criteria)):
        findings.append(Finding(requirement_id=req.id, kind=FindingKind.AMBIGUOUS,
                                message=f"Vague wording '{m.group(0)}' cannot be tested; state a measurable condition."))
        break
    if not cases and not findings:
        findings.append(Finding(requirement_id=req.id, kind=FindingKind.NOT_AUTOMATABLE,
                                message="No testable statement was recognized."))
    return GenerationResult(cases, findings)


def _is_context_sentence(sentence: str) -> bool:
    """Explanatory sentences without any normative word are not reported as unresolved."""
    return not re.search(r"\b(must|shall|should|can|cannot|may|will|required|mandatory|need|needs)\b", sentence, re.I)


def find_conflicts(per_req: dict[str, list[TestCase]], labels: dict[str, str]) -> list[Finding]:
    """Two requirements demanding opposite things about the same target."""
    out: list[Finding] = []
    index: dict[tuple, list[tuple[str, Expectation]]] = {}
    for rid, cases in per_req.items():
        for c in cases:
            t = c.check.target
            key = (c.check.kind, _norm(str(t.get("role") or t.get("field") or t.get("enum") or t.get("type") or "")),
                   t.get("permission") or t.get("from_status") or "", t.get("to_status") or "")
            index.setdefault(key, []).append((rid, c.check.expect))
    for key, items in index.items():
        for a, b in CONFLICT_PAIRS:
            ra = {r for r, e in items if e == a}
            rb = {r for r, e in items if e == b}
            for x in ra:
                for y in rb:
                    if x != y:
                        out.append(Finding(requirement_id=x, kind=FindingKind.CONFLICT, related=[y],
                                           message=f"{labels.get(x, x)} requires {a.value} but {labels.get(y, y)} "
                                                   f"requires {b.value} for the same target ({key[0].value})."))
    return out
