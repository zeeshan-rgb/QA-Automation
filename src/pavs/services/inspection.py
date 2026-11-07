"""Configuration inspection for the UI: run, store, load, compare, check a permission."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.config import ConfigSnapshot, PermissionCheck
from ..domain.errors import ConfigurationError
from ..engines.inspection import AREAS, InspectionContext, InspectionEngine, check_permission
from ..infra.storage import AuditLog, SnapshotRepository
from .connections import ConnectionService
from .roles import Action

# the attribute that identifies an item inside each named list (for diffs)
ITEM_KEYS = {
    "types": ("id",), "link_roles": ("id",), "effective": ("target_type", "id", "prototype"),
    "layers": ("scope", "target_type", "id"), "enumerations": ("enum_id", "target_type"),
    "rules": ("scope", "role", "permission", "effect", "kind", "view", "target_type", "ref"),
    "customsets": ("scope", "id"), "roles": ("scope", "id"), "assignments": ("scope", "principal", "role"),
    "files": ("prototype", "scope", "target_type"), "item_actions": ("work_item_type", "action_id"),
    "documents": ("space", "name"), "document_types": ("id",), "document_statuses": ("id",),
    "pages": ("space", "name"),
}


@dataclass
class SectionDiff:
    area: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _key(list_name: str, item: dict) -> str:
    fields = ITEM_KEYS.get(list_name, ("id",))
    return list_name + ":" + "|".join(str(item.get(f, "")) for f in fields if f in item)


def diff_snapshots(old: ConfigSnapshot, new: ConfigSnapshot) -> list[SectionDiff]:
    out = []
    for area in sorted({s.area for s in old.sections} | {s.area for s in new.sections}):
        a, b = old.section(area), new.section(area)
        d = SectionDiff(area)
        ia = {_key(n, i): i for n, lst in (a.items if a else {}).items() for i in lst}
        ib = {_key(n, i): i for n, lst in (b.items if b else {}).items() for i in lst}
        strip = lambda i: {k: v for k, v in i.items() if k != "sources"}  # noqa: E731
        d.added = sorted(k for k in ib if k not in ia)
        d.removed = sorted(k for k in ia if k not in ib)
        d.changed = sorted(k for k in ia if k in ib and strip(ia[k]) != strip(ib[k]))
        out.append(d)
    return out


class InspectionService:
    def __init__(self, connections: ConnectionService, snapshots: SnapshotRepository, audit: AuditLog,
                 engine: InspectionEngine | None = None) -> None:
        self.connections = connections
        self.snapshots = snapshots
        self.audit = audit
        self.engine = engine or InspectionEngine()

    @property
    def areas(self) -> list[str]:
        return list(AREAS)

    def _session(self, env_id: str, progress=None):
        session = self.connections.session(env_id)
        if session is None:
            env = self.connections.environments.get(env_id)
            if progress:
                progress("Connecting", 0)
            self.connections.connect(env)
            session = self.connections.session(env_id)
        if session is None:
            raise ConfigurationError("Could not open a session for this environment.")
        return session

    def inspect(self, env_id: str, project_id: str, areas: list[str] | None = None,
                progress=None, sample_items: bool = True) -> tuple[int, ConfigSnapshot]:
        """Blocking: run from a worker thread. Read-only."""
        self.connections.environments._need(Action.RUN_TESTS)
        session = self._session(env_id, progress)
        snap = self.engine.inspect(session, project_id, areas, progress, sample_items=sample_items)
        snap_id = self.snapshots.add(snap)
        self.audit.record("inspection.run", "ok", environment_id=env_id, target=project_id,
                          detail={"snapshot": snap_id, "sections": {s.area: s.status.value for s in snap.sections}})
        return snap_id, snap

    def history(self, env_id: str, project_id: str) -> list[tuple[int, str, str | None]]:
        return self.snapshots.list(env_id, project_id)

    def load(self, snap_id: int) -> ConfigSnapshot | None:
        return self.snapshots.get(snap_id)

    def compare(self, old_id: int, new_id: int) -> list[SectionDiff]:
        old, new = self.snapshots.get(old_id), self.snapshots.get(new_id)
        if old is None or new is None:
            raise ConfigurationError("Snapshot not found.")
        return diff_snapshots(old, new)

    def check_permission(self, env_id: str, project_id: str, user: str, permission: str) -> PermissionCheck:
        session = self._session(env_id)
        result = check_permission(InspectionContext(session=session, project_id=project_id), user, permission)
        self.audit.record("permission.check", "ok" if result.granted is not None else "failed",
                          environment_id=env_id, target=project_id,
                          detail={"user": user, "permission": permission, "granted": result.granted})
        return result
