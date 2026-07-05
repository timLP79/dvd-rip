import asyncio

import pytest

from app.scan_coordinator import ScanBusyError, ScanCoordinator


class FakeProc:
    """Stand-in for asyncio subprocess: has returncode + kill()."""
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True


def test_busy_true_during_run_false_after():
    c = ScanCoordinator()
    observed = {}

    async def factory(register_proc):
        observed["during"] = c.busy
        return "ok"

    result = asyncio.run(c.run(factory))
    assert observed["during"] is True
    assert c.busy is False
    assert result == "ok"


def test_run_raises_when_already_busy():
    c = ScanCoordinator()
    c._busy = True  # simulate a scan already running

    async def factory(register_proc):
        return "result"

    with pytest.raises(ScanBusyError):
        asyncio.run(c.run(factory))


def test_run_releases_busy_on_exception():
    c = ScanCoordinator()

    async def boom(register_proc):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        asyncio.run(c.run(boom))
    assert c.busy is False


def test_cancel_kills_registered_proc_and_sets_flag():
    c = ScanCoordinator()
    proc = FakeProc(returncode=None)

    async def scenario():
        async def factory(register_proc):
            register_proc(proc)
            return await c.cancel()  # cancel from within the running scan
        return await c.run(factory)

    cancelled = asyncio.run(scenario())
    assert cancelled is True
    assert proc.killed is True
    assert c.cancelled is True


def test_cancel_when_idle_returns_false():
    c = ScanCoordinator()
    assert asyncio.run(c.cancel()) is False


def test_cancel_does_not_kill_finished_proc():
    c = ScanCoordinator()
    proc = FakeProc(returncode=0)  # already exited

    async def scenario():
        async def factory(register_proc):
            register_proc(proc)
            return await c.cancel()
        return await c.run(factory)

    cancelled = asyncio.run(scenario())
    assert cancelled is True          # a scan WAS running
    assert proc.killed is False       # but the proc had already exited
