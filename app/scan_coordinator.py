"""Serializes /api/scan and makes an in-flight scan cancelable.

One scan touches the optical drive at a time. cancel() kills the live scan
subprocess (lsdvd/makemkvcon/HandBrake) so a hung scan on a bad disc can be
aborted instead of grinding the drive for minutes. It does NOT cancel the
request task — killing the subprocess lets the probe return naturally so the
HTTP response stays clean.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


class ScanBusyError(Exception):
    """Raised by ScanCoordinator.run when a scan is already in progress."""


class ScanCoordinator:
    def __init__(self) -> None:
        self._busy: bool = False
        self._proc: Optional[Any] = None  # asyncio subprocess of the live probe
        self._cancelled: bool = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def register_proc(self, proc: Any) -> None:
        """Record the current scan subprocess so cancel() can kill it.

        Probes run sequentially, so only one process is live at a time; each
        call replaces the previous handle.
        """
        self._proc = proc

    async def run(self, factory: Callable[[Callable], Awaitable[Any]]) -> Any:
        """Run one scan under the single-scan guard.

        `factory(register_proc)` performs the probe sequence. Raises
        ScanBusyError if a scan is already running. State is always cleared,
        even on exception, so a crash never wedges the busy flag.
        """
        if self._busy:
            raise ScanBusyError()
        self._busy = True
        self._cancelled = False
        self._proc = None
        try:
            return await factory(self.register_proc)
        finally:
            self._busy = False
            self._proc = None

    async def cancel(self) -> bool:
        """Kill the live scan subprocess, if any. Returns True if a scan was running."""
        if not self._busy:
            return False
        self._cancelled = True
        proc = self._proc
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return True
