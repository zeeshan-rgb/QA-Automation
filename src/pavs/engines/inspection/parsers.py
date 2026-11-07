"""Pure parsers for Polarion configuration XML (no I/O).

Formats verified against the Polarion 2606 demo repository (2026-11-06):
security/roles.xml, project-roles.xml, user-roles.xml, permissions.xml, projects.xml,
tracker/fields/*-enum.xml, *custom-fields.xml, tracker|documents|testing workflow XML,
hats/*/tracker/*-read-only-fields.xml and *-form-filter.xml.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ...infra import safexml
from ...domain.config import (CustomSet, EnumOptionDef, FieldDef, FieldRule, PermissionRule,
                              RoleAssignment, RoleDef, RuleCall, Scope, Source, Transition,
                              WorkflowActionDef, WorkflowDef)


class ConfigParseError(ValueError):
    pass


def _root(text: str, path: str) -> ET.Element:
    try:
        root = safexml.fromstring(text)
    except ET.ParseError as exc:
        raise ConfigParseError(f"{path}: invalid XML ({exc})") from None
    # drop namespaces (form-filter uses one)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() == "true"


# ---------------------------------------------------------------- projects / roles
def parse_projects(text: str, path: str = "projects.xml") -> dict[str, str]:
    """global .polarion/projects.xml -> {project id: repository path}."""
    return {p.get("id", ""): p.get("path", "") for p in _root(text, path).iter("project") if p.get("id")}


def parse_role_list(text: str, scope: Scope, source: Source) -> list[RoleDef]:
    """security/roles.xml (global) or security/project-roles.xml."""
    return [RoleDef(id=(r.text or "").strip(), scope=scope, sources=[source])
            for r in _root(text, source.location).iter("role") if (r.text or "").strip()]


def parse_user_roles(text: str, scope: Scope, source: Source) -> list[RoleAssignment]:
    out = []
    for user in _root(text, source.location).iter("user"):
        uid = user.get("name", "")
        for role in user.iter("role"):
            out.append(RoleAssignment(principal=uid, role=role.get("name", ""), scope=scope, sources=[source]))
    for group in _root(text, source.location).iter("group"):
        gid = group.get("name", "")
        for role in group.iter("role"):
            out.append(RoleAssignment(principal=gid, principal_kind="group", role=role.get("name", ""),
                                      scope=scope, sources=[source]))
    return out


def parse_permissions(text: str, scope: Scope, source: Source) -> tuple[list[PermissionRule], list[CustomSet]]:
    """Two styles exist in one file:
      <permission name="com.polarion"><role name="x"><grant permission="persistence.object.X.read"/>
      <role name="x"><grant permission="com.polarion.persistence.object.X.read"/>   (top level)
    A permission element's name is a prefix joined with "." to each grant.
    """
    root = _root(text, source.location)
    rules: list[PermissionRule] = []
    sets = [CustomSet(id=c.get("id", ""), title=c.get("title", ""), query=(c.text or "").strip(), scope=scope)
            for c in root.findall("customset")]

    def collect(role_el: ET.Element, prefix: str | None) -> None:
        role = role_el.get("name", "")
        for g in role_el:
            if g.tag not in ("grant", "deny"):
                continue
            perm = g.get("permission", "")
            # "@customset.action" refers to a customset that already holds the full object path
            full = perm if (not prefix or perm.startswith("@")) else f"{prefix}.{perm}"
            rules.append(PermissionRule(role=role, permission=full, effect=g.tag, scope=scope, sources=[source]))

    for child in root:
        if child.tag == "permission":
            prefix = child.get("name", "")
            for role_el in child.findall("role"):
                collect(role_el, prefix)
        elif child.tag == "role":
            collect(child, None)
    return rules, sets


# ---------------------------------------------------------------- enumerations & fields
_ENUM_FILE = re.compile(r"(?:^|/)(?:(?P<type>[^/]+?)-)?(?P<enum>[^/-][^/]*?)-enum\.xml$")


def enum_file_parts(filename: str, known_types: set[str] | None = None) -> tuple[str | None, str]:
    """'requirement-status-enum.xml' -> ('requirement', 'status'); 'workitem-type-enum.xml' ->
    (None, 'workitem-type'). Enum ids may contain '-', so a prefix only counts as a type when it
    is a known Work Item type (if the caller knows them)."""
    name = filename.rsplit("/", 1)[-1]
    if not name.endswith("-enum.xml"):
        raise ValueError(filename)
    stem = name[: -len("-enum.xml")]
    if known_types:
        for t in sorted(known_types, key=len, reverse=True):
            if stem.startswith(t + "-") and len(stem) > len(t) + 1:
                return t, stem[len(t) + 1:]
        return None, stem
    return None, stem


def parse_enum_options(text: str, path: str) -> list[EnumOptionDef]:
    out = []
    for o in _root(text, path).iter("option"):
        known = {"id", "name", "description", "default", "hidden", "sortOrder", "iconURL", "color",
                 "oppositeName", "parent", "terminal"}
        out.append(EnumOptionDef(
            id=o.get("id", ""), name=o.get("name", ""), description=o.get("description", ""),
            default=_bool(o.get("default")), hidden=_bool(o.get("hidden")),
            sort_order=int(o.get("sortOrder")) if (o.get("sortOrder") or "").isdigit() else None,
            icon=o.get("iconURL", ""), color=o.get("color", ""), opposite_name=o.get("oppositeName", ""),
            parent=_bool(o.get("parent")), terminal=_bool(o.get("terminal")),
            rules=[(r.get("from", ""), r.get("to", "")) for r in o.findall("rule")],
            extra={k: v for k, v in o.attrib.items() if k not in known}))
    return out


def field_enum_id(type_str: str) -> str | None:
    """'enum:priority' -> 'priority'; 'enum:@user[project_assignable]' -> '@user'."""
    if not type_str.startswith("enum:"):
        return None
    return type_str[5:].split("[", 1)[0]


def parse_custom_fields(text: str, target_type: str | None, scope: Scope, source: Source) -> list[FieldDef]:
    out = []
    for f in _root(text, source.location).iter("field"):
        t = f.get("type", "")
        out.append(FieldDef(
            id=f.get("id", ""), name=f.get("name", ""), type=t, enum_id=field_enum_id(t),
            required=_bool(f.get("required")), multi=_bool(f.get("multi")), default=f.get("default"),
            description=f.get("description", ""), depends_on=f.get("dependsOn"),
            target_type=target_type, scope=scope, sources=[source]))
    return out


def custom_fields_target(filename: str) -> str | None:
    """'custom-fields.xml' -> None, 'requirement-custom-fields.xml' -> 'requirement'."""
    name = filename.rsplit("/", 1)[-1]
    if name == "custom-fields.xml":
        return None
    if name.endswith("-custom-fields.xml"):
        return name[: -len("-custom-fields.xml")]
    raise ValueError(filename)


# ---------------------------------------------------------------- UI rules
def parse_read_only_fields(text: str, target_type: str | None, scope: Scope, source: Source) -> list[FieldRule]:
    out = []
    for st in _root(text, source.location).iter("status"):
        fields = [f.get("id", "") for f in st.iter("field") if f.get("id")]
        out.append(FieldRule(kind="read_only", target_type=target_type, ref=st.get("id", ""),
                             fields=fields, scope=scope, sources=[source]))
    return out


def parse_form_filter(text: str, target_type: str | None, scope: Scope, source: Source) -> list[FieldRule]:
    root = _root(text, source.location)
    out = []
    for tag, kind in (("action", "form_filter_action"), ("state", "form_filter_state")):
        for el in root.iter(tag):
            names = [n.strip() for n in el.get("fieldNames", "").split(",") if n.strip()]
            out.append(FieldRule(kind=kind, target_type=target_type, ref=el.get("refId", ""),
                                 fields=[n for n in names if not n.startswith("-")],
                                 excluded=[n[1:] for n in names if n.startswith("-")],
                                 scope=scope, sources=[source]))
    return out


def hat_file_target(filename: str, suffix: str) -> str | None:
    """'requirement-read-only-fields.xml' -> 'requirement'; 'form-filter.xml' -> None."""
    name = filename.rsplit("/", 1)[-1]
    if name == suffix:
        return None
    if name.endswith("-" + suffix):
        return name[: -len(suffix) - 1]
    raise ValueError(filename)


# ---------------------------------------------------------------- workflow
def _rule_calls(parent: ET.Element | None, tag: str) -> list[RuleCall]:
    if parent is None:
        return []
    return [RuleCall(name=c.get("name", ""), params={p.get("name", ""): p.get("value", "") for p in c.findall("param")})
            for c in parent.findall(tag)]


def parse_workflow(text: str, target_type: str | None, scope: Scope, source: Source) -> WorkflowDef:
    root = _root(text, source.location)
    if root.tag != "workflow":
        raise ConfigParseError(f"{source.location}: root element is <{root.tag}>, expected <workflow>")
    actions = []
    for a in root.iter("action"):
        req = a.find("required")
        clr = a.find("cleared")
        roles_el = a.find("roles")
        sig = a.find("signatures")
        policy = None
        auto = False
        if sig is not None:
            p = sig.find("signature-policy")
            policy = p.get("name") if p is not None else None
            auto = sig.find("auto-signature") is not None
        actions.append(WorkflowActionDef(
            id=a.get("id", ""), name=a.get("name", ""),
            required_fields=[f.get("name", "") for f in req.findall("field")] if req is not None else [],
            cleared_fields=[f.get("name", "") for f in clr.findall("field")] if clr is not None else [],
            required_roles=[r.get("name", "") or (r.text or "").strip() for r in roles_el.findall("role")]
            if roles_el is not None else [],
            conditions=_rule_calls(a.find("conditions"), "condition"),
            functions=_rule_calls(a.find("functions"), "function"),
            signature_policy=policy, auto_signature=auto))
    transitions = [Transition(action=t.get("action", ""), source=t.get("from", ""), target=t.get("to", ""))
                   for t in root.iter("transition")]
    return WorkflowDef(prototype=root.get("prototype", "WorkItem"), target_type=target_type, scope=scope,
                       initial_status=root.get("initial-status"), initial_action=root.get("initial-action"),
                       actions=actions, transitions=transitions, sources=[source])


def workflow_file_target(filename: str) -> str | None:
    """'workflow.xml' -> None, 'requirement-workflow.xml' -> 'requirement'."""
    name = filename.rsplit("/", 1)[-1]
    if name == "workflow.xml":
        return None
    if name.endswith("-workflow.xml"):
        return name[: -len("-workflow.xml")]
    raise ValueError(filename)
