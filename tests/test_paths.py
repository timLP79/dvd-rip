from pathlib import Path

import pytest

from app.paths import movie_path, sanitize, tv_episode_path, unique_path


def test_movie_path_with_year(tmp_path):
    p = movie_path(tmp_path, title="The Matrix", year=1999)
    assert p == tmp_path / "Movies" / "The Matrix (1999)" / "The Matrix (1999).mp4"


def test_movie_path_without_year(tmp_path):
    p = movie_path(tmp_path, title="The Matrix", year=None)
    assert p == tmp_path / "Movies" / "The Matrix" / "The Matrix.mp4"


def test_movie_path_multipart_uses_cd_suffix(tmp_path):
    p1 = movie_path(tmp_path, title="Dune", year=2021, part_index=1)
    p2 = movie_path(tmp_path, title="Dune", year=2021, part_index=2)
    p3 = movie_path(tmp_path, title="Dune", year=2021, part_index=3)
    assert p1.name == "Dune (2021).mp4"
    assert p2.name == "Dune (2021) - cd2.mp4"
    assert p3.name == "Dune (2021) - cd3.mp4"
    # All three live in the same folder
    assert p1.parent == p2.parent == p3.parent


def test_tv_episode_path_matches_user_convention(tmp_path):
    """Deadwood S01E01 layout from the user's existing library."""
    p = tv_episode_path(tmp_path, show="Deadwood", season=1, episode=1)
    assert p == tmp_path / "TV Shows" / "Deadwood" / "Season 01" / "Deadwood S01E01.mp4"


def test_tv_episode_path_zero_pads(tmp_path):
    p = tv_episode_path(tmp_path, show="Breaking Bad", season=5, episode=14)
    assert p.name == "Breaking Bad S05E14.mp4"
    assert p.parent.name == "Season 05"


def test_ext_param_controls_container(tmp_path):
    # Blu-ray rips pass ext="mkv"; DVD keeps the default mp4.
    m = movie_path(tmp_path, title="The Martian", year=2015, ext="mkv")
    assert m.name == "The Martian (2015).mkv"
    mp = movie_path(tmp_path, title="Dune", year=2021, part_index=2, ext="mkv")
    assert mp.name == "Dune (2021) - cd2.mkv"
    tv = tv_episode_path(tmp_path, show="Deadwood", season=1, episode=1, ext="mkv")
    assert tv.name == "Deadwood S01E01.mkv"


def test_tv_episode_path_double_digit_season(tmp_path):
    p = tv_episode_path(tmp_path, show="Doctor Who", season=12, episode=3)
    assert p.parent.name == "Season 12"
    assert p.name == "Doctor Who S12E03.mp4"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The Matrix", "The Matrix"),
        ("Star Wars: A New Hope", "Star Wars A New Hope"),
        ("Spider-Man / Spider-Man 2", "Spider-Man Spider-Man 2"),
        ("Foo?Bar*Baz", "FooBarBaz"),
        ("...", "Untitled"),
        ("  Multiple   Spaces  ", "Multiple Spaces"),
        ("", "Untitled"),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_unique_path_appends_counter(tmp_path):
    path = tmp_path / "movie.mp4"
    assert unique_path(path) == path
    path.write_text("")
    assert unique_path(path).name == "movie_2.mp4"
    (tmp_path / "movie_2.mp4").write_text("")
    assert unique_path(path).name == "movie_3.mp4"
