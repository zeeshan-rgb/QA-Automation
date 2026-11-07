"""Fake Polarion SVN configuration (format copied from a real 2606 repository, content invented)."""
from __future__ import annotations

P = "Acme/acme_demo/.polarion"     # project acme_demo lives under /Acme/acme_demo
T = "acme_test/.polarion"          # project acme_test lives under /acme_test

FILES: dict[str, str] = {
    ".polarion/projects.xml": """<?xml version="1.0" encoding="UTF-8"?>
<projects>
    <project id="acme_demo" path="/Acme/acme_demo"/>
    <project id="acme_test" path="/acme_test"/>
</projects>""",
    ".polarion/security/roles.xml": """<?xml version="1.0" encoding="UTF-8"?>
<roles>
    <role>admin</role>
    <role>user</role>
</roles>""",
    ".polarion/security/project-roles.xml": """<?xml version="1.0" encoding="UTF-8"?>
<roles>
  <role>project_user</role>
  <role>project_assignable</role>
  <role>project_admin</role>
  <role>internal_reviewer</role>
</roles>""",
    ".polarion/security/user-roles.xml": """<?xml version="1.0" encoding="UTF-8"?>
<user-roles>
    <user name="admin">
        <role name="admin"/>
        <role name="user"/>
    </user>
    <user name="reviewer1">
        <role name="user"/>
    </user>
</user-roles>""",
    ".polarion/security/permissions.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<permissions>
    <customset id="Module_Released" title="Released Specifications">com.polarion.persistence.object.Module(status:released)</customset>
    <permission name="com.polarion.persistence.object.Revision">
        <role name="user">
            <grant permission="read"/>
        </role>
    </permission>
    <role name="user">
        <grant permission="com.polarion.security.login"/>
    </role>
    <role name="project_user">
        <grant permission="com.polarion.persistence.object.WorkItem.read"/>
    </role>
    <role name="project_assignable">
        <grant permission="com.polarion.persistence.object.WorkItem.read"/>
        <grant permission="com.polarion.persistence.object.WorkItem.modify"/>
        <deny permission="@Module_Released.modifyContent"/>
    </role>
</permissions>""",
    f"{P}/security/user-roles.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<user-roles>
    <user name="admin">
        <role name="project_admin"/>
    </user>
    <user name="reviewer1">
        <role name="internal_reviewer"/>
        <role name="project_user"/>
    </user>
    <user name="tester1">
        <role name="internal_reviewer"/>
    </user>
    <user name="dev1">
        <role name="project_assignable"/>
    </user>
    <user name="roapi1">
        <role name="internal_reviewer"/>
    </user>
</user-roles>""",
    f"{P}/security/permissions.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<permissions>
    <permission name="com.polarion">
        <role name="internal_reviewer">
            <grant permission="persistence.object.WorkItem.read"/>
            <grant permission="persistence.object.Comment.create"/>
            <deny permission="persistence.object.WorkItem.modify"/>
            <deny permission="persistence.object.WorkItem.delete"/>
        </role>
    </permission>
</permissions>""",
    ".polarion/tracker/fields/workitem-type-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option id="requirement" name="Requirement" sortOrder="1" default="true"/>
    <option id="task" name="Task" sortOrder="2"/>
    <option id="testcase" name="Test Case" sortOrder="3"/>
</enumeration>""",
    ".polarion/tracker/fields/workitem-link-role-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option default="true" id="relates_to" name="relates to" oppositeName="is related to" sortOrder="1"/>
    <option id="verifies" name="verifies" oppositeName="is verified by" sortOrder="2">
        <rule from="testcase" to="requirement"/>
    </option>
    <option id="parent" name="has parent" oppositeName="is parent of" parent="true" sortOrder="3"/>
</enumeration>""",
    ".polarion/tracker/fields/status-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option id="open" name="Open" default="true"/>
    <option id="done" name="Done" terminal="true"/>
</enumeration>""",
    ".polarion/tracker/fields/priority-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option id="high" name="High"/>
    <option id="low" name="Low" default="true"/>
</enumeration>""",
    f"{P}/tracker/fields/requirement-status-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option id="draft" name="Draft" default="true"/>
    <option id="reviewed" name="Reviewed"/>
    <option id="approved" name="Approved" terminal="true"/>
</enumeration>""",
    ".polarion/tracker/fields/custom-fields.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<fields>
    <field id="businessValue" name="Business Value" type="integer"/>
</fields>""",
    f"{P}/tracker/fields/requirement-custom-fields.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<fields>
    <field id="reviewer" name="Reviewer" type="enum:@user[project_assignable]" required="true"/>
    <field id="priorityClass" name="Priority Class" type="enum:priority" multi="true"/>
    <field default="false" id="safetyRelevant" name="Safety Relevant" type="boolean"/>
</fields>""",
    ".polarion/tracker/workflow/workflow.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<workflow initial-action="init" initial-status="open" prototype="WorkItem">
    <transitions>
        <transition action="close" from="open" to="done"/>
        <transition action="reopen" from="done" to="open"/>
    </transitions>
    <actions>
        <action id="init" name="Initialization"/>
        <action id="close" name="Close"/>
        <action id="reopen" name="Reopen"/>
    </actions>
</workflow>""",
    f"{P}/tracker/workflow/requirement-workflow.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<workflow initial-action="rework" initial-status="draft" prototype="WorkItem">
    <transitions>
        <transition action="review" from="draft" to="reviewed"/>
        <transition action="approve" from="reviewed" to="approved"/>
        <transition action="rework" from="reviewed" to="draft"/>
        <transition action="rework" from="approved" to="draft"/>
    </transitions>
    <actions>
        <action id="rework" name="Rework">
            <cleared>
                <field name="resolution"/>
            </cleared>
        </action>
        <action id="review" name="Review">
            <required>
                <field name="reviewer"/>
            </required>
        </action>
        <action id="approve" name="Approve">
            <conditions>
                <condition name="FieldNonEmpty">
                    <param name="field" value="reviewer"/>
                </condition>
            </conditions>
            <functions>
                <function name="SetFieldValue">
                    <param name="field" value="resolution"/>
                    <param name="value" value="done"/>
                </function>
            </functions>
        </action>
    </actions>
</workflow>""",
    ".polarion/documents/fields/document-type-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option id="req_specification" name="Requirements Specification" default="true"/>
    <option id="generic" name="Generic"/>
</enumeration>""",
    ".polarion/documents/fields/document-status-enum.xml": """<?xml version="1.0" encoding="UTF-8"?>
<enumeration>
    <option id="draft" name="Draft" default="true"/>
    <option id="released" name="Released"/>
</enumeration>""",
    ".polarion/documents/workflow/workflow.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<workflow initial-status="draft" prototype="Module">
    <transitions>
        <transition action="release" from="draft" to="released"/>
    </transitions>
    <actions>
        <action id="release" name="Release">
            <signatures>
                <signature-policy name="all"/>
            </signatures>
        </action>
    </actions>
</workflow>""",
    f"{P}/hats/_default/tracker/requirement-read-only-fields.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<read-only>
    <status id="_default">
        <field id="title"/>
    </status>
    <status id="approved">
        <field id="title"/>
        <field id="description"/>
    </status>
</read-only>""",
    f"{P}/hats/_default/tracker/requirement-form-filter.xml": """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<formFilter xmlns="http://polarion.com/schema/WorkItems/FormFilter">
<workflow>
<actions>
    <action fieldNames="@required,description,reviewer" refId="_create"/>
</actions>
<states>
    <state fieldNames="@required,description,-severity" refId="_default"/>
</states>
</workflow>
</formFilter>""",
    f"{P}/pages/widgets/demo/render.vm": """## demo widget
#set($items = $transaction.workItems.search.query("type:requirement"))
#foreach($i in $items)
  #set($old = $i.getOldApi())
  <li>$old.getTitle()</li>
#if($items.size() > 0)
  <p>never closed</p>
""",
    f"{P}/pages/spaces/Reports/Coverage/page.xml": """<?xml version="1.0" encoding="UTF-8"?>
<rich-page><field id="homePageContent" text-type="text/html"><![CDATA[<p><span data-widget="com.polarion.scriptInline"><sub id="script"><sub id="script" style="white-space:pre;">#set($q = &quot;status:*open&quot;)
#set($r = $transaction.workItems.search.query(&quot;type:task AND title:*bug&quot;))
$r.size()</sub></sub></span></p>]]></field></rich-page>""",
    f"{T}/security/user-roles.xml": """<?xml version="1.0" encoding="UTF-8"?>
<user-roles>
    <user name="admin">
        <role name="project_admin"/>
    </user>
</user-roles>""",
}

# Fake data that the API side of the mock serves for acme_demo
DOCUMENTS = [("Specifications", "System Requirements", "System Requirements", "req_specification", "draft"),
             ("Specifications", "Release Notes", "Release Notes", "generic", "released")]
PAGES = [("_default", "Home", "Home"), ("Reports", "Coverage", "Coverage Report")]
WORK_ITEMS = {"requirement": ("AD-1", "draft"), "task": ("AD-2", "open")}


def project_root(project_id: str) -> str | None:
    return {"acme_demo": P, "acme_test": T}.get(project_id)
