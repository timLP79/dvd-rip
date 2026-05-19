from app.ripper import _PROGRESS_RE


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
