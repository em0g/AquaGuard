"""Tests for database retention policies."""

from __future__ import annotations

from aquaguard.storage import database as db_mod


class TestPressureTestRetention:
    async def test_old_curves_dropped_summaries_kept(self, database, monkeypatch):
        monkeypatch.setattr(db_mod, "_PRESSURE_TEST_CURVES_KEPT", 3)
        for i in range(5):
            await database.save_pressure_test(
                test_type="scheduled", result="pass",
                initial_pressure=5.0, final_pressure=4.9,
                values_stage1=[1.0, 2.0], values_stage2=[3.0, 4.0],
            )

        tests = await database.get_pressure_tests(limit=10)
        assert len(tests) == 5  # summary rows are kept forever
        newest_first = tests
        with_curves = [t for t in newest_first if t["values_stage1"] is not None]
        without = [t for t in newest_first if t["values_stage1"] is None]
        assert len(with_curves) == 3
        assert len(without) == 2
        # The stripped ones are the oldest
        assert all(t["id"] <= 2 for t in without)


class TestEpisodeRetention:
    async def test_old_episodes_pruned(self, database):
        # Insert an episode dated beyond the retention window via raw SQL
        await database._execute(
            "INSERT INTO episode (timestamp, volume, duration) "
            "VALUES (datetime('now', '-400 days'), 12.5, 60)"
        )
        await database.save_episode(volume=1.0, duration=10)

        episodes = await database.get_episodes(limit=10)
        assert len(episodes) == 1
        assert episodes[0]["volume"] == 1.0


class TestLastScheduledDate:
    async def test_empty_db_returns_none(self, database):
        assert await database.get_last_scheduled_test_date() is None

    async def test_manual_tests_ignored(self, database):
        await database.save_pressure_test(
            test_type="manual", result="pass",
            initial_pressure=5.0, final_pressure=4.9,
        )
        assert await database.get_last_scheduled_test_date() is None
