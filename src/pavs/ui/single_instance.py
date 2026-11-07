"""One window per data folder: starting the app again brings the open one to the front.

`--new` (or the "New window" button) starts a separate, independent copy on purpose. Instances are told apart by the
data folder, so a test copy with its own PAVS_DATA_DIR never collides with the real one.
"""
from __future__ import annotations

import hashlib
from typing import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..infra import paths


def instance_name(data_dir: str | None = None) -> str:
    key = str(data_dir if data_dir is not None else paths.data_dir()).lower().replace("\\", "/").rstrip("/")
    return "pavs-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def forward_to_running(name: str, timeout_ms: int = 400) -> bool:
    """True when another instance is running; it has been asked to show its window."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"raise")
    sock.waitForBytesWritten(timeout_ms)
    sock.waitForReadyRead(timeout_ms * 3)               # the running copy answers "ok" once it has shown its window
    sock.disconnectFromServer()
    return True


def listen(name: str, on_raise: Callable[[], None]) -> QLocalServer | None:
    """Become the primary instance: answer 'raise' requests from later launches."""
    QLocalServer.removeServer(name)                       # a crashed run may have left its name behind
    server = QLocalServer()
    if not server.listen(name):
        return None

    def handle() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read(c=conn) -> None:
            if c.bytesAvailable():
                c.readAll()
                on_raise()
                c.write(b"ok")
                c.flush()

        conn.readyRead.connect(read)
        conn.disconnected.connect(conn.deleteLater)
        read()                                            # the request may have arrived before the signal was connected

    server.newConnection.connect(handle)
    return server
