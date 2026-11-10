"""SQLite persistence behind small repository classes (PostgreSQL can be added later)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..domain.models import Environment, ServerProfile

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE environments (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE server_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        connected_at TEXT NOT NULL,
        release TEXT,
        data TEXT NOT NULL
    );
    CREATE INDEX ix_profiles_env ON server_profiles(environment_id, connected_at);
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        environment_id TEXT,
        action TEXT NOT NULL,
        target TEXT,
        outcome TEXT NOT NULL,
        detail TEXT
    );
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """,
    # 2: Phase 2 configuration snapshots
    """
    CREATE TABLE config_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL,
        taken_at TEXT NOT NULL,
        release TEXT,
        data TEXT NOT NULL
    );
    CREATE INDEX ix_snapshots_env ON config_snapshots(environment_id, project_id, id);
    """,
    # 3: Phase 3 requirements, generated test cases, findings and test runs
    """
    CREATE TABLE requirement_sets (id TEXT PRIMARY KEY, name TEXT NOT NULL, imported_at TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE requirements (id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES requirement_sets(id) ON DELETE CASCADE, data TEXT NOT NULL);
    CREATE TABLE test_cases (id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES requirement_sets(id) ON DELETE CASCADE,
        requirement_id TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE findings (id INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT NOT NULL REFERENCES requirement_sets(id) ON DELETE CASCADE, data TEXT NOT NULL);
    CREATE TABLE test_runs (id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, project_id TEXT NOT NULL,
        started_at TEXT NOT NULL, verdict TEXT NOT NULL, data TEXT NOT NULL);
    CREATE INDEX ix_runs_env ON test_runs(environment_id, project_id, started_at);
    """,
    # 4: Phase 4 test data created in test projects (for guaranteed cleanup)
    """
    CREATE TABLE test_artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, environment_id TEXT NOT NULL,
        project_id TEXT NOT NULL, ref TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT, data TEXT NOT NULL);
    CREATE INDEX ix_artifacts_open ON test_artifacts(environment_id, project_id, deleted_at);
    """,
    # 5: application role and other local settings
    """
    CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 6: role tests: objects created in a test project (for guaranteed cleanup) and role assignments waiting to be restored
    """
    CREATE TABLE roletest_artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, environment_id TEXT NOT NULL,
        project_id TEXT NOT NULL, kind TEXT NOT NULL, uri TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT);
    CREATE INDEX ix_roletest_open ON roletest_artifacts(environment_id, project_id, deleted_at);
    CREATE TABLE roletest_pending (run_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, project_id TEXT NOT NULL, data TEXT NOT NULL,
        created_at TEXT NOT NULL);
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version, script in enumerate(MIGRATIONS, start=1):
                if version > current:
                    self._conn.executescript("BEGIN;" + script + f"PRAGMA user_version={version}; COMMIT;")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @property
    def schema_version(self) -> int:
        return self.query("PRAGMA user_version")[0][0]

    def close(self) -> None:
        self._conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnvironmentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> list[Environment]:
        rows = self.db.query("SELECT data FROM environments ORDER BY name COLLATE NOCASE")
        return [Environment.model_validate_json(r["data"]) for r in rows]

    def get(self, env_id: str) -> Environment | None:
        rows = self.db.query("SELECT data FROM environments WHERE id=?", (env_id,))
        return Environment.model_validate_json(rows[0]["data"]) if rows else None

    def save(self, env: Environment) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO environments(id, name, data, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data, "
                "updated_at=excluded.updated_at",
                (env.id, env.name, env.model_dump_json(), _now()))

    def delete(self, env_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM environments WHERE id=?", (env_id,))


class ProfileRepository:
    """Connection snapshots. Exchanges are kept (already redacted) as evidence."""

    def __init__(self, db: Database, keep_per_env: int = 20) -> None:
        self.db = db
        self.keep = keep_per_env

    def add(self, profile: ServerProfile) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO server_profiles(environment_id, connected_at, release, data) VALUES(?,?,?,?)",
                (profile.environment_id, profile.connected_at.isoformat(),
                 profile.version.release, profile.model_dump_json()))
            c.execute(
                "DELETE FROM server_profiles WHERE environment_id=? AND id NOT IN ("
                "SELECT id FROM server_profiles WHERE environment_id=? ORDER BY id DESC LIMIT ?)",
                (profile.environment_id, profile.environment_id, self.keep))

    def latest(self, env_id: str) -> ServerProfile | None:
        rows = self.db.query(
            "SELECT data FROM server_profiles WHERE environment_id=? ORDER BY id DESC LIMIT 1", (env_id,))
        return ServerProfile.model_validate_json(rows[0]["data"]) if rows else None

    def history(self, env_id: str) -> list[tuple[str, str | None]]:
        rows = self.db.query(
            "SELECT connected_at, release FROM server_profiles WHERE environment_id=? ORDER BY id DESC",
            (env_id,))
        return [(r["connected_at"], r["release"]) for r in rows]


class SnapshotRepository:
    """Configuration snapshots per environment + project (kept for history and diffs)."""

    def __init__(self, db: Database, keep_per_project: int = 30) -> None:
        self.db = db
        self.keep = keep_per_project

    def add(self, snap) -> int:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO config_snapshots(environment_id, project_id, taken_at, release, data) "
                "VALUES(?,?,?,?,?)",
                (snap.environment_id, snap.project_id, snap.taken_at.isoformat(), snap.release,
                 snap.model_dump_json()))
            new_id = cur.lastrowid
            c.execute(
                "DELETE FROM config_snapshots WHERE environment_id=? AND project_id=? AND id NOT IN ("
                "SELECT id FROM config_snapshots WHERE environment_id=? AND project_id=? "
                "ORDER BY id DESC LIMIT ?)",
                (snap.environment_id, snap.project_id, snap.environment_id, snap.project_id, self.keep))
        return int(new_id)

    def list(self, env_id: str, project_id: str) -> list[tuple[int, str, str | None]]:
        rows = self.db.query(
            "SELECT id, taken_at, release FROM config_snapshots WHERE environment_id=? AND project_id=? "
            "ORDER BY id DESC", (env_id, project_id))
        return [(r["id"], r["taken_at"], r["release"]) for r in rows]

    def get(self, snap_id: int):
        from ..domain.config import ConfigSnapshot
        rows = self.db.query("SELECT data FROM config_snapshots WHERE id=?", (snap_id,))
        return ConfigSnapshot.model_validate_json(rows[0]["data"]) if rows else None

    def clear(self, env_id: str | None = None, project_id: str | None = None) -> int:
        sql, args, conds = "DELETE FROM config_snapshots", [], []
        if env_id:
            conds.append("environment_id=?")
            args.append(env_id)
        if project_id:
            conds.append("project_id=?")
            args.append(project_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        with self.db.tx() as c:
            return c.execute(sql, tuple(args)).rowcount


class RequirementRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add_set(self, rs, reqs: list, cases: list) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO requirement_sets(id, name, imported_at, data) VALUES(?,?,?,?)",
                      (rs.id, rs.name, rs.imported_at.isoformat(), rs.model_dump_json()))
            for r in reqs:
                c.execute("INSERT INTO requirements(id, set_id, data) VALUES(?,?,?)", (r.id, rs.id, r.model_dump_json()))
            for t in cases:
                c.execute("INSERT INTO test_cases(id, set_id, requirement_id, data) VALUES(?,?,?,?)",
                          (t.id, rs.id, t.requirement_id, t.model_dump_json()))

    def list_sets(self) -> list:
        from ..domain.requirements import RequirementSet
        rows = self.db.query("SELECT data FROM requirement_sets ORDER BY imported_at DESC")
        return [RequirementSet.model_validate_json(r["data"]) for r in rows]

    def get_set(self, set_id: str):
        from ..domain.requirements import RequirementSet
        rows = self.db.query("SELECT data FROM requirement_sets WHERE id=?", (set_id,))
        return RequirementSet.model_validate_json(rows[0]["data"]) if rows else None

    def requirements(self, set_id: str) -> list:
        from ..domain.requirements import Requirement
        rows = self.db.query("SELECT data FROM requirements WHERE set_id=? ORDER BY rowid", (set_id,))
        return [Requirement.model_validate_json(r["data"]) for r in rows]

    def cases(self, set_id: str) -> list:
        from ..domain.requirements import TestCase
        rows = self.db.query("SELECT data FROM test_cases WHERE set_id=? ORDER BY rowid", (set_id,))
        return [TestCase.model_validate_json(r["data"]) for r in rows]

    def findings(self, set_id: str) -> list:
        from ..domain.requirements import Finding
        rows = self.db.query("SELECT data FROM findings WHERE set_id=? ORDER BY id", (set_id,))
        return [Finding.model_validate_json(r["data"]) for r in rows]

    def replace_generated(self, set_id: str, cases: list, findings: list) -> None:
        """Replace rule-generated cases and findings; manual (structured-spec) cases are kept."""
        with self.db.tx() as c:
            for row in c.execute("SELECT id, data FROM test_cases WHERE set_id=?", (set_id,)).fetchall():
                if '"origin":"rule_generated"' in row["data"].replace(" ", ""):
                    c.execute("DELETE FROM test_cases WHERE id=?", (row["id"],))
            for t in cases:
                c.execute("INSERT OR REPLACE INTO test_cases(id, set_id, requirement_id, data) VALUES(?,?,?,?)",
                          (t.id, set_id, t.requirement_id, t.model_dump_json()))
            c.execute("DELETE FROM findings WHERE set_id=?", (set_id,))
            for f in findings:
                c.execute("INSERT INTO findings(set_id, data) VALUES(?,?)", (set_id, f.model_dump_json()))

    def update_case(self, tc) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE test_cases SET data=? WHERE id=?", (tc.model_dump_json(), tc.id))

    def delete_set(self, set_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM requirement_sets WHERE id=?", (set_id,))


class TestRunRepository:
    __test__ = False

    def __init__(self, db: Database, keep_per_project: int = 50) -> None:
        self.db = db
        self.keep = keep_per_project

    def add(self, run) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO test_runs(id, environment_id, project_id, started_at, verdict, data) "
                      "VALUES(?,?,?,?,?,?)", (run.id, run.environment_id, run.project_id,
                                              run.started_at.isoformat(), run.verdict, run.model_dump_json()))
            c.execute("DELETE FROM test_runs WHERE environment_id=? AND project_id=? AND id NOT IN ("
                      "SELECT id FROM test_runs WHERE environment_id=? AND project_id=? ORDER BY started_at DESC LIMIT ?)",
                      (run.environment_id, run.project_id, run.environment_id, run.project_id, self.keep))

    def list(self, env_id: str | None = None, project_id: str | None = None, limit: int = 100) -> list:
        from ..domain.testing import TestRunRecord
        sql, args = "SELECT data FROM test_runs", []
        conds = []
        if env_id:
            conds.append("environment_id=?")
            args.append(env_id)
        if project_id:
            conds.append("project_id=?")
            args.append(project_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = self.db.query(sql + " ORDER BY started_at DESC LIMIT ?", tuple(args) + (limit,))
        return [TestRunRecord.model_validate_json(r["data"]) for r in rows]

    def get(self, run_id: str):
        from ..domain.testing import TestRunRecord
        rows = self.db.query("SELECT data FROM test_runs WHERE id=?", (run_id,))
        return TestRunRecord.model_validate_json(rows[0]["data"]) if rows else None

    def delete(self, run_ids: list[str]) -> int:
        with self.db.tx() as c:
            return sum(c.execute("DELETE FROM test_runs WHERE id=?", (i,)).rowcount for i in run_ids)

    def clear(self, env_id: str | None = None, project_id: str | None = None) -> int:
        """Delete stored test runs (everything, or one environment / project). Test-data cleanup records are not touched."""
        sql, args, conds = "DELETE FROM test_runs", [], []
        if env_id:
            conds.append("environment_id=?")
            args.append(env_id)
        if project_id:
            conds.append("project_id=?")
            args.append(project_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        with self.db.tx() as c:
            return c.execute(sql, tuple(args)).rowcount


class ArtifactRepository:
    """Every test Work Item the tool creates, until it has been deleted again."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, art) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO test_artifacts(id, run_id, environment_id, project_id, ref, created_at, deleted_at, data) "
                      "VALUES(?,?,?,?,?,?,?,?)", (art.id, art.run_id, art.environment_id, art.project_id, art.ref,
                                                  art.created_at.isoformat(), None, art.model_dump_json()))

    def mark_deleted(self, art_id: str) -> None:
        from ..domain.testing import TestArtifact
        with self.db.tx() as c:
            row = c.execute("SELECT data FROM test_artifacts WHERE id=?", (art_id,)).fetchone()
            if row is None:
                return
            art = TestArtifact.model_validate_json(row["data"])
            art.deleted_at = _now_dt()
            c.execute("UPDATE test_artifacts SET deleted_at=?, data=? WHERE id=?",
                      (art.deleted_at.isoformat(), art.model_dump_json(), art_id))

    def open_items(self, env_id: str, project_id: str | None = None, run_id: str | None = None) -> list:
        from ..domain.testing import TestArtifact
        sql, args = "SELECT data FROM test_artifacts WHERE environment_id=? AND deleted_at IS NULL", [env_id]
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        return [TestArtifact.model_validate_json(r["data"]) for r in self.db.query(sql + " ORDER BY rowid", tuple(args))]


def _now_dt():
    return datetime.now(timezone.utc)


class AuditLog:
    def __init__(self, db: Database, actor: str) -> None:
        self.db = db
        self.actor = actor

    def record(self, action: str, outcome: str, environment_id: str | None = None,
               target: str | None = None, detail: dict | str | None = None) -> None:
        from .redaction import scrub
        text = json.dumps(detail, default=str) if isinstance(detail, dict) else detail
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO audit_log(at, actor, environment_id, action, target, outcome, detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (_now(), self.actor, environment_id, action, target, outcome, scrub(text)))

    def recent(self, limit: int = 200) -> list[dict]:
        rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


class RoleTestRepository:
    """Objects a role test created (until deleted again) and role assignments that still have to be restored."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- created objects
    def add_artifact(self, run_id: str, env_id: str, project_id: str, kind: str, uri: str, title: str) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO roletest_artifacts(run_id, environment_id, project_id, kind, uri, title, created_at) VALUES(?,?,?,?,?,?,?)",
                      (run_id, env_id, project_id, kind, uri, title, _now()))

    def open_artifacts(self, env_id: str, project_id: str, run_id: str | None = None) -> list[tuple[str, str, str, str]]:
        """(run_id, kind, uri, title) of everything not yet deleted."""
        sql, args = "SELECT run_id, kind, uri, title FROM roletest_artifacts WHERE environment_id=? AND project_id=? AND deleted_at IS NULL", [env_id, project_id]
        if run_id:
            sql, args = sql + " AND run_id=?", args + [run_id]
        return [tuple(r) for r in self.db.query(sql + " ORDER BY id", tuple(args))]

    def done_artifact(self, env_id: str, project_id: str, uri: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE roletest_artifacts SET deleted_at=? WHERE environment_id=? AND project_id=? AND uri=? AND deleted_at IS NULL",
                      (_now(), env_id, project_id, uri))

    # ---- role assignments to restore
    def set_pending(self, run_id: str, env_id: str, project_id: str, data: str) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO roletest_pending(run_id, environment_id, project_id, data, created_at) VALUES(?,?,?,?,?)",
                      (run_id, env_id, project_id, data, _now()))

    def pending(self, env_id: str | None = None) -> list[tuple[str, str, str, str]]:
        """(run_id, environment_id, project_id, data)"""
        sql, args = "SELECT run_id, environment_id, project_id, data FROM roletest_pending", ()
        if env_id:
            sql, args = sql + " WHERE environment_id=?", (env_id,)
        return [tuple(r) for r in self.db.query(sql + " ORDER BY created_at", args)]

    def clear_pending(self, run_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM roletest_pending WHERE run_id=?", (run_id,))
