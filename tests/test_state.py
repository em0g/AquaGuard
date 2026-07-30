"""Tests for the JSON state store."""

from __future__ import annotations

from aquaguard.storage.state import StateStore


class TestStateStore:
    async def test_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(str(path))
        await store.load()
        await store.set("valve_open", False)
        await store.set("alarm_type", "leak_detected")

        fresh = StateStore(str(path))
        await fresh.load()
        assert fresh.get("valve_open") is False
        assert fresh.get("alarm_type") == "leak_detected"

    async def test_atomic_write_leaves_no_tempfile(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(str(path))
        await store.load()
        await store.set("valve_open", True)

        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == []

    async def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ this is not json", encoding="utf-8")

        store = StateStore(str(path))
        await store.load()
        assert store.get("valve_open") is True  # default
        assert store.get("alarm_active") is False
