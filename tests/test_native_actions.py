import plistlib
from pathlib import Path
from unittest import mock

import native_actions


def test_verify_app_installed_finds_existing(tmp_path, monkeypatch):
    fake_apps = tmp_path / "Applications"
    fake_apps.mkdir()
    (fake_apps / "Foo.app").mkdir()

    real_path = Path

    def fake_path(p, *args, **kwargs):
        if p == "/Applications":
            return real_path(fake_apps, *args, **kwargs)
        return real_path(p, *args, **kwargs)

    with mock.patch.object(native_actions, "Path", side_effect=fake_path) as mp:
        mp.home.return_value = tmp_path
        is_error, body = native_actions.verify_app_installed("Foo")
    assert is_error is False
    assert "Foo.app" in body


def test_verify_app_installed_missing(tmp_path, monkeypatch):
    real_path = Path

    def fake_path(p, *args, **kwargs):
        if p == "/Applications":
            return real_path(tmp_path / "Applications-missing", *args, **kwargs)
        return real_path(p, *args, **kwargs)

    with mock.patch.object(native_actions, "Path", side_effect=fake_path) as mp:
        mp.home.return_value = tmp_path
        is_error, body = native_actions.verify_app_installed("DoesNotExist")
    assert is_error is True
    assert "not found" in body


def test_attached_volumes_parses_plist(monkeypatch):
    fixture = plistlib.dumps({
        "images": [
            {
                "image-path": "/Users/me/Desktop/gc12.dmg",
                "system-entities": [
                    {"mount-point": "/Volumes/GraphicConverter 12"},
                    {"content-hint": "no mount point here"},
                ],
            },
            {
                "image-path": "/other.dmg",
                "system-entities": [{"mount-point": "/Volumes/Other"}],
            },
        ],
    })

    class FakeProc:
        returncode = 0
        stdout = fixture

    monkeypatch.setattr(native_actions.subprocess, "run", lambda *a, **kw: FakeProc())
    volumes = native_actions.attached_volumes()
    mounts = {v["mount_point"] for v in volumes}
    assert mounts == {"/Volumes/GraphicConverter 12", "/Volumes/Other"}


def test_find_app_in_volume_prefers_named(tmp_path):
    (tmp_path / "Other.app").mkdir()
    (tmp_path / "GraphicConverter 12.app").mkdir()
    found = native_actions.find_app_in_volume(tmp_path, app_name="GraphicConverter 12")
    assert found.name == "GraphicConverter 12.app"


def test_find_app_in_volume_falls_back_to_first(tmp_path):
    (tmp_path / "Only.app").mkdir()
    found = native_actions.find_app_in_volume(tmp_path)
    assert found.name == "Only.app"


def test_safe_shell_refuses_destructive():
    is_error, body = native_actions.safe_shell("rm -rf /Users/me")
    assert is_error is True
    assert "Refused" in body


def test_safe_shell_refuses_outside_allowlist():
    is_error, body = native_actions.safe_shell("curl http://example.com")
    assert is_error is True
    assert "allow-list" in body


def test_safe_shell_allows_hdiutil(monkeypatch):
    monkeypatch.setattr(native_actions, "run_cmd", lambda *a, **kw: (0, "ok"))
    is_error, body = native_actions.safe_shell("hdiutil info")
    assert is_error is False
    assert body == "ok"
