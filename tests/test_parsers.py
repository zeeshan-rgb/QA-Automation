"""Parser tests. Inputs use the real Polarion file formats (verified on a 2606 repository)."""
import pytest

from pavs.domain.config import Scope, Source
from pavs.domain.enums import ProviderKind
from pavs.engines.inspection import parsers as P

SRC = Source(provider=ProviderKind.SVN, location="/x.xml", scope=Scope.GLOBAL)

PERMISSIONS = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<permissions>
    <customset id="Module_InProcess" title="In process">com.polarion.persistence.object.Module(type:spec AND NOT status:draft)</customset>
    <permission name="com.polarion.persistence.object.Revision">
        <role name="user"><grant permission="read"/></role>
    </permission>
    <permission name="com.polarion">
        <role name="project_admin">
            <grant permission="persistence.object.ModuleComment(.module:(@Module_InProcess)).create"/>
            <deny permission="@Module_InProcess.modifyContent"/>
        </role>
    </permission>
    <role name="self">
        <grant permission="com.polarion.persistence.object.User.read"/>
        <deny permission="com.polarion.persistence.object.User.key.time-splitAssignments.modify"/>
    </role>
</permissions>"""


def test_permissions_both_styles_and_customsets():
    rules, sets = P.parse_permissions(PERMISSIONS, Scope.PROJECT, SRC)
    perms = {(r.role, r.effect, r.permission) for r in rules}
    assert ("user", "grant", "com.polarion.persistence.object.Revision.read") in perms
    assert ("project_admin", "grant",
            "com.polarion.persistence.object.ModuleComment(.module:(@Module_InProcess)).create") in perms
    assert ("project_admin", "deny", "@Module_InProcess.modifyContent") in perms      # no prefix on @sets
    assert ("self", "deny", "com.polarion.persistence.object.User.key.time-splitAssignments.modify") in perms
    assert sets[0].id == "Module_InProcess" and "status:draft" in sets[0].query
    deny = next(r for r in rules if r.effect == "deny" and r.role == "project_admin")
    assert deny.customsets == ["Module_InProcess"]


def test_roles_and_user_roles():
    roles = P.parse_role_list("<roles><role>project_user</role><role> project_admin </role></roles>",
                              Scope.GLOBAL, SRC)
    assert [r.id for r in roles] == ["project_user", "project_admin"]
    ua = P.parse_user_roles('<user-roles><user name="u1"><role name="a"/><role name="b"/></user>'
                            '<group name="g1"><role name="c"/></group></user-roles>', Scope.PROJECT, SRC)
    assert [(a.principal, a.principal_kind, a.role) for a in ua] == [
        ("u1", "user", "a"), ("u1", "user", "b"), ("g1", "group", "c")]


def test_enum_options_and_link_rules():
    xml = """<enumeration>
      <option default="true" id="relates_to" name="relates to" oppositeName="is related to" sortOrder="1"/>
      <option id="parent" name="has parent" oppositeName="is parent of" parent="true" sortOrder="2">
        <rule from="task" to="task"/><rule from="requirement" to="requirement"/>
      </option>
      <option id="x" name="X" color="#fff" createDefect="true"/>
    </enumeration>"""
    opts = P.parse_enum_options(xml, "e.xml")
    assert opts[0].default and opts[0].opposite_name == "is related to" and opts[0].sort_order == 1
    assert opts[1].parent and opts[1].rules == [("task", "task"), ("requirement", "requirement")]
    assert opts[2].extra == {"createDefect": "true"} and opts[2].color == "#fff"


@pytest.mark.parametrize("name,types,expected", [
    ("requirement-status-enum.xml", {"requirement"}, ("requirement", "status")),
    ("workitem-type-enum.xml", {"requirement"}, (None, "workitem-type")),
    ("workitem-link-role-enum.xml", None, (None, "workitem-link-role")),
    ("softwarerequirement-severity-enum.xml", {"softwarerequirement"}, ("softwarerequirement", "severity")),
])
def test_enum_file_names(name, types, expected):
    assert P.enum_file_parts(name, types) == expected


def test_custom_fields():
    xml = """<fields>
      <field id="targetVersion" name="Target Version" type="enum:@plan[template.id:release]"/>
      <field default="false" id="noCov" name="Not covered" type="boolean"/>
      <field id="area" name="Area" type="enum:processArea" multi="true" required="true" dependsOn="domain"/>
    </fields>"""
    fs = P.parse_custom_fields(xml, "requirement", Scope.PROJECT, SRC)
    assert [(f.id, f.enum_id) for f in fs] == [("targetVersion", "@plan"), ("noCov", None), ("area", "processArea")]
    assert fs[1].default == "false" and fs[2].multi and fs[2].required and fs[2].depends_on == "domain"
    assert P.custom_fields_target("custom-fields.xml") is None
    assert P.custom_fields_target("issue-custom-fields.xml") == "issue"


WORKFLOW = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<workflow initial-action="init" initial-status="open" prototype="WorkItem">
  <transitions>
    <transition action="start" from="open" to="inprogress"/>
    <transition action="done" from="inprogress" to="done"/>
  </transitions>
  <actions>
    <action id="init" name="Initialization"><cleared><field name="resolution"/></cleared></action>
    <action id="start" name="Start"><required><field name="assignee"/></required></action>
    <action id="done" name="Done">
      <signatures><signature-policy name="all"/></signatures>
      <conditions><condition name="LinkedWorkItemsStatus"><param name="link.roles" value="depends_on"/></condition></conditions>
      <functions><function name="InvokeAction"><param name="actionId" value="approve"/></function></functions>
    </action>
  </actions>
</workflow>"""


def test_workflow():
    wf = P.parse_workflow(WORKFLOW, "task", Scope.GLOBAL, SRC)
    assert wf.prototype == "WorkItem" and wf.initial_status == "open" and wf.initial_action == "init"
    assert wf.statuses == ["open", "inprogress", "done"]
    assert wf.action("start").required_fields == ["assignee"]
    assert wf.action("init").cleared_fields == ["resolution"]
    done = wf.action("done")
    assert done.signature_policy == "all"
    assert done.conditions[0].name == "LinkedWorkItemsStatus" and done.conditions[0].params == {"link.roles": "depends_on"}
    assert done.functions[0].params == {"actionId": "approve"}
    assert P.workflow_file_target("workflow.xml") is None
    assert P.workflow_file_target("systemReqSpecification-workflow.xml") == "systemReqSpecification"
    with pytest.raises(P.ConfigParseError):
        P.parse_workflow("<notworkflow/>", None, Scope.GLOBAL, SRC)
    with pytest.raises(P.ConfigParseError):
        P.parse_workflow("<workflow", None, Scope.GLOBAL, SRC)


def test_ui_rules():
    ro = P.parse_read_only_fields('<read-only><status id="_default"><field id="title"/></status>'
                                  '<status id="_create"><field id=""/></status></read-only>', "req", Scope.PROJECT, SRC)
    assert [(r.ref, r.fields) for r in ro] == [("_default", ["title"]), ("_create", [])]
    ff = P.parse_form_filter(
        '<formFilter xmlns="http://polarion.com/schema/WorkItems/FormFilter"><workflow><actions>'
        '<action fieldNames="@required,description" refId="_create"/></actions><states>'
        '<state fieldNames="@required,-type,categories" refId="_default"/></states></workflow></formFilter>',
        "req", Scope.PROJECT, SRC)
    assert [(r.kind, r.ref, r.fields, r.excluded) for r in ff] == [
        ("form_filter_action", "_create", ["@required", "description"], []),
        ("form_filter_state", "_default", ["@required", "categories"], ["type"])]
    assert P.hat_file_target("form-filter.xml", "form-filter.xml") is None
    assert P.hat_file_target("risk-form-filter.xml", "form-filter.xml") == "risk"


def test_projects_xml():
    assert P.parse_projects('<projects><project id="a" path="/G/a"/></projects>') == {"a": "/G/a"}
