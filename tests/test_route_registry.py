"""Route-registry loader tests (Batch 4 N1).

The nerve_navigate route table moved out of a hardcoded dict in ui_tools into
``routes.yaml`` (env-overridable via NERVE_ROUTES_FILE) with a hardcoded
fallback in navigation_tools. These tests lock the three load paths.
"""

from __future__ import annotations

import pathlib

import yaml

from linc_nerve.mcp import navigation_tools as nav


def test_bundled_yaml_matches_hardcoded_fallback() -> None:
    """The shipped routes.yaml must mirror the _FALLBACK_APPS dict exactly.

    They are two copies of the same data by design (the fallback exists so a
    corrupt/missing yaml never hard-fails navigation) — so drift between them is
    a bug this test catches.
    """
    data = yaml.safe_load(nav._ROUTES_FILE.read_text(encoding="utf-8"))
    assert data["apps"] == nav._FALLBACK_APPS


def test_load_from_bundled_yaml() -> None:
    """Default load path (no env override) reads the bundled yaml."""
    registry, packages = nav._load_route_registry()
    assert registry["precisionlevel"]["settings"] == "precisionlevel://screen/settings"
    assert packages["precisionlevel"] == "com.lincmobile.level"
    assert set(registry) == {"precisionlevel", "sniperpulse"}


def test_env_override(tmp_path: pathlib.Path, monkeypatch) -> None:
    """NERVE_ROUTES_FILE points the loader at a custom yaml."""
    custom = tmp_path / "routes.yaml"
    custom.write_text(
        yaml.safe_dump(
            {
                "apps": {
                    "dawncup": {
                        "package": "com.linc.dawncup",
                        "routes": {"home": "dawncup://home"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NERVE_ROUTES_FILE", str(custom))
    registry, packages = nav._load_route_registry()
    assert registry == {"dawncup": {"home": "dawncup://home"}}
    assert packages == {"dawncup": "com.linc.dawncup"}


def test_missing_file_falls_back(tmp_path: pathlib.Path, monkeypatch) -> None:
    """A missing override path degrades to the hardcoded fallback, not a crash."""
    monkeypatch.setenv("NERVE_ROUTES_FILE", str(tmp_path / "does-not-exist.yaml"))
    registry, packages = nav._load_route_registry()
    assert set(registry) == set(nav._FALLBACK_APPS)
    assert packages["sniperpulse"] == "com.lincmobile.sniperpulse"


def test_corrupt_yaml_falls_back(tmp_path: pathlib.Path, monkeypatch) -> None:
    """An unparseable / schema-less yaml degrades to the fallback."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("apps: []\n", encoding="utf-8")  # 'apps' present but not a mapping
    monkeypatch.setenv("NERVE_ROUTES_FILE", str(bad))
    registry, _packages = nav._load_route_registry()
    assert set(registry) == set(nav._FALLBACK_APPS)
