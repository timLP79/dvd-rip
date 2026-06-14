from collections import deque
from pathlib import Path

from app.ripper import Ripper, _PROGRESS_RE


def test_progress_regex_with_eta():
    line = "Encoding: task 1 of 1, 42.50 % (35.10 fps, avg 32.45 fps, ETA 00h08m12s)"
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group("pct") == "42.50"
    assert m.group("fps") == "35.10"
    assert m.group("h") == "00"
    assert m.group("m") == "08"
    assert m.group("s") == "12"


def test_progress_regex_without_eta():
    line = "Encoding: task 1 of 1, 12.34 %"
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group("pct") == "12.34"
    assert m.group("fps") is None


def test_progress_regex_ignores_other_output():
    line = "Scanning title 1 of 5"
    assert _PROGRESS_RE.search(line) is None


def test_handle_line_collects_non_progress_into_tail():
    """Non-progress output (errors, warnings) is retained for failure diagnostics."""
    ripper = Ripper(device="/dev/null", output_root=Path("/tmp"))
    tail: deque[str] = deque(maxlen=25)

    # A progress line does NOT go into the tail.
    ripper._handle_line("nojob", "Encoding: task 1 of 1, 5.00 %", tail)
    assert len(tail) == 0

    # Error/warning lines DO accumulate (stripped, non-empty only).
    ripper._handle_line("nojob", "libdvdcss: could not get title key", tail)
    ripper._handle_line("nojob", "   ", tail)  # blank -> skipped
    ripper._handle_line("nojob", "No space left on device", tail)
    assert list(tail) == [
        "libdvdcss: could not get title key",
        "No space left on device",
    ]


def test_handle_line_tail_is_bounded():
    ripper = Ripper(device="/dev/null", output_root=Path("/tmp"))
    tail: deque[str] = deque(maxlen=25)
    for i in range(100):
        ripper._handle_line("nojob", f"log line {i}", tail)
    assert len(tail) == 25
    assert list(tail)[-1] == "log line 99"
