"""Host-platform layer: device naming, tool selection, storage defaults.

These are the seams where a wrong value silently misbehaves rather than crashing
(a mis-named device makes a disc look absent; a mis-translated libdiscid id makes
an audio CD look like a DVD), so they get direct coverage.
"""

import pytest

from app import hostenv


# Real `drutil status` output, disc inserted, from an external USB drive.
DRUTIL_WITH_DISC = """ Vendor   Product           Rev
 HL-DT-ST DVDRAM GP60NB60   RA00

           Type: DVD-ROM               Name: /dev/disk4
       Sessions: 1                  Tracks: 1
   Overwritable:   00:00:00         blocks:        0 /   0.00MB /   0.00MiB
     Space Free:   00:00:00         blocks:        0 /   0.00MB /   0.00MiB
     Space Used:  100:00:00         blocks:        0 /   0.00MB /   0.00MiB
    Writability:
      Book Type: DVD-ROM (v1)
"""

# Drive attached, tray empty. The drive is still named, which is what lets the app
# report "no disc" instead of "no drive".
DRUTIL_NO_DISC = """ Vendor   Product           Rev
 HL-DT-ST DVDRAM GP60NB60   RA00

           Type: No Media              Name: /dev/disk4
"""


def test_parse_drutil_finds_device_with_disc():
    assert hostenv.parse_drutil_device(DRUTIL_WITH_DISC) == "/dev/disk4"


def test_parse_drutil_finds_drive_with_empty_tray():
    assert hostenv.parse_drutil_device(DRUTIL_NO_DISC) == "/dev/disk4"


def test_parse_drutil_no_drive_attached():
    """`drutil status` prints nothing at all when there is no optical drive."""
    assert hostenv.parse_drutil_device("") is None


def test_parse_drutil_ignores_unrelated_output():
    assert hostenv.parse_drutil_device("some other tool's output\n") is None


def test_raw_device_maps_to_character_node():
    assert hostenv.raw_device("/dev/disk4") == "/dev/rdisk4"
    assert hostenv.raw_device("/dev/disk11") == "/dev/rdisk11"


def test_raw_device_leaves_other_paths_alone():
    """An explicit override must never be rewritten."""
    assert hostenv.raw_device("/dev/sr0") == "/dev/sr0"
    assert hostenv.raw_device("/dev/rdisk4") == "/dev/rdisk4"


async def test_resolve_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("DVD_DEVICE", "/dev/sr9")
    # Detection must not even run when the user has pinned a device.
    async def _boom():
        raise AssertionError("detection ran despite DVD_DEVICE override")
    monkeypatch.setattr(hostenv, "detect_optical_device", _boom)
    assert await hostenv.resolve_optical_device() == "/dev/sr9"


async def test_resolve_falls_back_when_nothing_detected(monkeypatch):
    monkeypatch.delenv("DVD_DEVICE", raising=False)

    async def _none():
        return None
    monkeypatch.setattr(hostenv, "detect_optical_device", _none)
    assert await hostenv.resolve_optical_device() == "/dev/sr0"


async def test_resolve_uses_detected_device(monkeypatch):
    monkeypatch.delenv("DVD_DEVICE", raising=False)

    async def _found():
        return "/dev/rdisk4"
    monkeypatch.setattr(hostenv, "detect_optical_device", _found)
    assert await hostenv.resolve_optical_device() == "/dev/rdisk4"


async def test_detect_returns_none_off_macos(monkeypatch):
    """On Linux the device is configured, not discovered — no subprocess at all."""
    monkeypatch.setattr(hostenv, "IS_MACOS", False)
    assert await hostenv.detect_optical_device() is None


async def test_device_label_reports_missing_drive(monkeypatch):
    """The header must not claim a /dev/sr0 that isn't there."""
    monkeypatch.delenv("DVD_DEVICE", raising=False)
    monkeypatch.setattr(hostenv, "IS_MACOS", True)

    async def _none():
        return None
    monkeypatch.setattr(hostenv, "detect_optical_device", _none)
    assert await hostenv.device_label() == "no drive detected"


async def test_device_label_shows_detected_drive(monkeypatch):
    monkeypatch.delenv("DVD_DEVICE", raising=False)
    monkeypatch.setattr(hostenv, "IS_MACOS", True)

    async def _found():
        return "/dev/rdisk4"
    monkeypatch.setattr(hostenv, "detect_optical_device", _found)
    assert await hostenv.device_label() == "/dev/rdisk4"


def test_discid_device_override_wins(monkeypatch):
    monkeypatch.setenv("DISCID_DEVICE", "2")
    assert hostenv.discid_device("/dev/rdisk4") == "2"


def test_discid_device_passthrough_on_linux(monkeypatch):
    monkeypatch.delenv("DISCID_DEVICE", raising=False)
    monkeypatch.setattr(hostenv, "IS_MACOS", False)
    assert hostenv.discid_device("/dev/sr0") == "/dev/sr0"


def test_discid_device_on_macos_is_not_a_path(monkeypatch):
    """macOS libdiscid takes a drive index; handing it /dev/... makes read() fail,
    which the scanner would misread as 'this isn't an audio CD'."""
    monkeypatch.delenv("DISCID_DEVICE", raising=False)
    monkeypatch.setattr(hostenv, "IS_MACOS", True)
    assert not hostenv.discid_device("/dev/rdisk4").startswith("/dev/")


def test_makemkv_source_defaults_to_dev_spec(monkeypatch):
    monkeypatch.delenv("MAKEMKV_SOURCE", raising=False)
    assert hostenv.makemkv_source("/dev/rdisk4") == "dev:/dev/rdisk4"


def test_makemkv_source_override(monkeypatch):
    """MakeMKV's drive addressing varies by platform/build; the override is the
    documented escape hatch when `dev:<path>` isn't recognised."""
    monkeypatch.setenv("MAKEMKV_SOURCE", "disc:0")
    assert hostenv.makemkv_source("/dev/rdisk4") == "disc:0"


def test_makemkvcon_bin_override(monkeypatch):
    monkeypatch.setenv("MAKEMKVCON_BIN", "/custom/makemkvcon")
    assert hostenv.makemkvcon_bin() == "/custom/makemkvcon"


@pytest.mark.parametrize("fn", [
    hostenv.default_output_root,
    hostenv.default_library_db,
    hostenv.default_scratch_dir,
])
def test_storage_defaults_are_absolute(fn):
    assert fn().startswith("/")


def test_ledger_and_scratch_stay_out_of_the_media_tree():
    """Plex scans OUTPUT_ROOT; library.db and scratch MKV/WAVs must not live there."""
    output = hostenv.default_output_root().rstrip("/") + "/"
    assert not hostenv.default_library_db().startswith(output)
    assert not hostenv.default_scratch_dir().startswith(output)


def test_speed_mode_availability_tracks_encoder():
    assert hostenv.speed_mode_available() is (hostenv.SPEED_ENCODER is not None)
