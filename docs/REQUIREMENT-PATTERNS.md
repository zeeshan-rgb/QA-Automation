# Requirement sentences the tool understands

The tool uses **no AI**. It matches each sentence of a requirement (and each acceptance criterion) against the fixed patterns below. A sentence that matches no pattern is **never guessed**: it is listed on the *Requirements & generated tests* tab as "not automatable". Vague wording ("fast", "user-friendly") is reported as "ambiguous". The code is `src/pavs/engines/requirements/rules.py`; every example below is covered by `tests/test_requirements.py`.

Names can be written as the display name ("Internal Reviewer") or the id (`internal_reviewer`); case and punctuation are ignored. A name is looked up in the configuration read from the project when the tests run.

| You write (examples) | Check generated | How it is checked |
|---|---|---|
| "Create a role called Internal Reviewer." / "The role Internal Reviewer exists." | Role exists | Role list of the project (SOAP, or `roles.xml` / `project-roles.xml`) |
| "Internal Reviewer **can** comment on Work Items." / "... **has permission to** read Work Items." | Permission granted | **Server answer** (SOAP `hasPermission`) for a real user who holds the role; if no such user, the permission files only, reported as *partial pass* |
| "Internal Reviewer **cannot** / **must not** / **should not have permission to** modify or delete them." | Permission denied (one check per verb) | Same as above |
| "The role ... should have permission to **review** Work Items" | Read + comment permission | Documented mapping: Polarion has no "review" permission, so *review* = `WorkItem.read` + `Comment.create` |
| "The field Reviewer is **mandatory** / **required** for Requirement work items." / "Saving a Requirement without Reviewer is rejected." | Field required | Custom field definition `required`; if only a workflow action requires it, *partial pass* |
| "The field X is **optional**." | Field optional | Same |
| "The field X is **read-only**." | Field read-only | Read-only field rules (needs the SVN reader; UI-only setting) |
| "Create a custom field called Safety Class of type Enumeration for Requirement work items." | Field exists (+ type) | Custom field definitions for that Work Item type |
| "Define a work item type called Risk." | Type exists | Work Item type enumeration |
| "The Priority enumeration must **contain** the values High, Low and Medium." | Enumeration options present | Enumeration options (also *must not contain*) |
| "Requirement work items **can move from** Draft **to** Reviewed." | Transition allowed | Workflow definition (needs the SVN reader) |
| "Requirement work items **cannot move from** Approved **to** Draft." | Transition blocked | Workflow definition |
| "The **review action requires** the field Reviewer for Requirement work items." / "Reviewer is required **when reviewing**." | Action requires field | Workflow action's required fields |
| "The workflow has a status called Escalated." / "The status becomes Reviewed." | Status exists | Status enumeration + workflow |
| "Add a link role called verifies." | Link role exists | Link role enumeration |
| "Add a document type called Specification." | Document type exists | Document type enumeration |
| "**Every** Requirement **must be linked to** a Test Case." | Traceability | **Measured on the real Work Items** (either link direction): PASS when all are linked, FAIL with the unlinked ids and the coverage %. If the Work Items cannot be read, it falls back to *partial pass* (the configuration allows such links) |
| Given / When / Then lines | One criterion per scenario; the sentences inside are matched as above | |

## Verdicts
- **PASS / FAIL**: a real comparison was made, with the evidence listed.
- **PARTIAL PASS**: checked from the configuration files only, or only part of the statement could be checked.
- **BLOCKED**: the data needed could not be read (for example, no SVN reader for workflow definitions). The result says what is missing and how to fix it.
- **NOT TESTABLE**: no automatic mechanism exists for it.

## What it does not do
- It does not interpret free-form prose, performance, usability or appearance requirements.
- It does not invent tests. Only what the requirement states is tested; "boundary" cases are not generated.
- By default it does not create or change anything in Polarion. With **Behavioural tests** switched on (and a test project approved) the permission and workflow-transition statements above are also **really tried**: transitions on a temporary Work Item, permissions as a test account. See the README for the safety rules.
- It cannot see what a user sees in the browser (LiveReports, form layouts): Phase 5.

## Changing or adding patterns
Edit the rule functions and vocabulary lists at the top of `rules.py`, add an example to `tests/test_requirements.py`, and update this table.
