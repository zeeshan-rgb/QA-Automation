"""Composition root: builds infrastructure, core and services (no Qt here)."""
from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path

from .infra import paths
from .infra.logging_setup import configure_logging
from .infra.storage import (AuditLog, Database, EnvironmentRepository, ProfileRepository,
                            ArtifactRepository, RequirementRepository, SnapshotRepository, TestRunRepository)
from .infra.vault import CredentialVault, VaultBackend
from .polarion.capabilities import CapabilityRegistry
from .polarion.connection import ConnectionManager
from .services.connections import ConnectionService
from .services.environments import EnvironmentService
from .integrations.tortoisesvn import TortoiseLauncher
from .services.inspection import InspectionService
from .services.merge import MergeService
from .services.roles import RoleService
from .services.testing import TestingService


@dataclass
class AppContext:
    db: Database
    audit: AuditLog
    vault: CredentialVault
    registry: CapabilityRegistry
    environments: EnvironmentService
    connections: ConnectionService
    inspection: InspectionService
    testing: TestingService
    roles: RoleService
    merge: MergeService

    def close(self) -> None:
        self.connections.disconnect_all()
        self.db.close()


def build_context(db_path: Path | str | None = None, vault_backend: VaultBackend | None = None,
                  http_transport=None, log_to_file: bool = True, svn_launcher: TortoiseLauncher | None = None) -> AppContext:
    configure_logging(paths.logs_dir() if log_to_file else None)
    launcher = svn_launcher or TortoiseLauncher()
    db = Database(db_path or paths.database_path())
    audit = AuditLog(db, actor=_actor())
    vault = CredentialVault(vault_backend)
    registry = CapabilityRegistry.load()
    envs = EnvironmentService(EnvironmentRepository(db), vault, audit)
    manager = ConnectionManager(registry=registry, audit=audit, http_transport=http_transport)
    conns = ConnectionService(envs, ProfileRepository(db), manager)
    inspection = InspectionService(conns, SnapshotRepository(db), audit)
    testing = TestingService(conns, inspection, RequirementRepository(db), TestRunRepository(db), audit, ArtifactRepository(db))
    roles = RoleService(db, audit)
    merge = MergeService(conns, TestRunRepository(db), audit, launcher)
    envs.roles = testing.roles = merge.roles = roles
    return AppContext(db, audit, vault, registry, envs, conns, inspection, testing, roles, merge)


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return "unknown"
