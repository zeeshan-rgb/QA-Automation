"""Run blocking service calls off the UI thread, and deliver results back ON the UI thread."""
from __future__ import annotations

import traceback
from functools import partial
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot

from ..infra.logging_setup import get_logger
from ..infra.redaction import scrub

log = get_logger("ui.workers")


class _Relay(QObject):
    """Lives in the UI thread. Any thread may emit `call`; the callable then runs in the UI thread."""
    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._invoke, Qt.QueuedConnection)

    @Slot(object)
    def _invoke(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("UI callback failed")


_relay: _Relay | None = None


def _get_relay() -> _Relay:
    global _relay
    if _relay is None:
        _relay = _Relay()          # created on first use, which is always the UI thread
    return _relay


class WorkerSignals(QObject):
    progress = Signal(str, int)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    """fn(progress_callback) -> result. Exceptions are reported as scrubbed text."""

    def __init__(self, fn: Callable[[Callable[[str, int], None]], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(lambda msg, pct: self.signals.progress.emit(msg, pct))
        except Exception as exc:
            log.error("worker failed: %s\n%s", exc, traceback.format_exc())
            self.signals.error.emit(scrub(f"{type(exc).__name__}: {exc}") or "error")
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


def run_in_background(fn, on_result=None, on_error=None, on_progress=None, on_finished=None) -> Worker:
    """Start fn in the thread pool. The on_* callbacks always run in the UI thread, in order."""
    relay = _get_relay()
    w = Worker(fn)

    def deliver(cb):
        # connected directly, so it runs in the worker thread; it only queues the real callback to the UI thread
        return lambda *args: relay.call.emit(partial(cb, *args))

    if on_progress:
        w.signals.progress.connect(deliver(on_progress), Qt.DirectConnection)
    if on_result:
        w.signals.result.connect(deliver(on_result), Qt.DirectConnection)
    if on_error:
        w.signals.error.connect(deliver(on_error), Qt.DirectConnection)
    if on_finished:
        w.signals.finished.connect(deliver(on_finished), Qt.DirectConnection)
    w.setAutoDelete(False)
    _keep.add(w)
    w.signals.finished.connect(lambda: relay.call.emit(partial(_keep.discard, w)), Qt.DirectConnection)
    QThreadPool.globalInstance().start(w)
    return w


_keep: set[Worker] = set()      # keeps workers (and their signal objects) alive until they have delivered
