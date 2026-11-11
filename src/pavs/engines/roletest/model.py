"""What one probe found, why it failed, and what to do about it."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    ALLOWED = "allowed"        # the server did it
    DENIED = "denied"          # the server refused: the user has no permission for it
    BLOCKED = "blocked"        # could not be tried: something in the project's set-up stands in the way
    ERROR = "error"            # an unexpected answer from the server


class Cause(str, Enum):
    NONE = ""
    PERMISSION = "permission"
    REPO_ACCESS = "repository access"
    WORKFLOW = "workflow script"
    REQUIRED_FIELD = "required field"
    SETUP = "test set-up"
    SESSION = "session"
    SERVER = "server"


ADVICE = {
    Cause.PERMISSION: "The role does not grant this. Add the permission to the role (Administration > User Management > Permissions), "
                      "or change the expected result if the role should not be able to do it.",
    Cause.REPO_ACCESS: "The user cannot read the project's repository folder. A role assigned by editing user-roles.xml does not update "
                       "Polarion's repository access list. Assign the role in Polarion itself (Administration > User Management) and run the test with "
                       "'The user already has the role(s)', or add the user to the project's groups in the repository's .polarion/access file.",
    Cause.WORKFLOW: "A workflow or save-hook script configured in the project could not run (script missing or failing). "
                    "Fix the script path in the project (workflow or save hooks); this is not a permission problem. A path that starts with a project id "
                    "that does not exist on the server usually means the project was copied or renamed: replace that id with this project's id.",
    Cause.REQUIRED_FIELD: "The workflow needs a field that is empty on the test Work Item. Make the field optional or give it a default value, "
                          "or fill it in by hand and retry.",
    Cause.SETUP: "The test could not prepare what it needs (see the detail). Fix the cause or test this part by hand.",
    Cause.SESSION: "The server dropped the session during the test. Run the test again.",
    Cause.SERVER: "The server answered with an unexpected error. See the detail; the Polarion log (polarion.log) has more.",
}


@dataclass
class Probe:
    group: str                 # Work Item type name in capitals, DOCUMENT, TEST RUN, COLLECTION
    action: str                # READ, CREATE NEW, MODIFY, ...
    outcome: Outcome
    cause: Cause = Cause.NONE
    detail: str = ""
    mechanism: str = "SOAP, signed in as the test user"


def classify(ok: bool, message=None) -> tuple[Outcome, Cause, str]:
    """(outcome, cause, short detail) for a SOAP call that either succeeded or raised a fault with `message`."""
    if ok:
        return Outcome.ALLOWED, Cause.NONE, ""
    m = str(message or "")
    low = m.lower()
    if ("access to the subversion repository" in low and ("no access" in low or "do not have access" in low)) or "repository read permission denied" in low:
        return Outcome.BLOCKED, Cause.REPO_ACCESS, "the user has no access to the project's repository folder"
    if "permissiondenied" in low or "denied for user" in low or low.startswith("permission "):
        what = m.split(" denied for user")[0].rsplit(".", 2)
        return Outcome.DENIED, Cause.PERMISSION, ".".join(what[-2:]).strip()[:120]
    if "script was not found" in low or "workflow initialization failed" in low or ("script" in low and "not found" in low):
        path = re.search(r"Script repository path: \[([^\]]+)\]", m)
        what = f"script '{path.group(1)}' was not found" if path else "a workflow or save-hook script of the project is missing or failing"
        return Outcome.BLOCKED, Cause.WORKFLOW, what + " (a save hook or workflow of the project runs it)"
    if "required field" in low:
        return Outcome.BLOCKED, Cause.REQUIRED_FIELD, m[:200]
    if low.strip(" .") == "not authorized":
        return Outcome.ERROR, Cause.SESSION, "the session was not accepted by the server"
    return Outcome.ERROR, Cause.SERVER, m[:200]
