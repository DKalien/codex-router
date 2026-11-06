import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from config import (
    DEFAULT_CONFIG,
    load_config,
    migrate_provider_keys,
    resolve_key_migration,
    save_config,
)
from provider_keys import ProviderKeyError


class TestConfigStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mimo_cfg_test_")
        self.data_dir = os.path.join(self._tmp, "data")
        self.legacy_dir = os.path.join(self._tmp, "legacy")
        self._orig_data_dir = os.environ.get("MIMO_TOKEN_MONITOR_DATA_DIR")
        self._orig_legacy_dir = os.environ.get("MIMO_LEGACY_CONFIG_DIR")
        os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = self.data_dir
        os.environ["MIMO_LEGACY_CONFIG_DIR"] = self.legacy_dir

    def tearDown(self):
        if self._orig_data_dir is None:
            os.environ.pop("MIMO_TOKEN_MONITOR_DATA_DIR", None)
        else:
            os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = self._orig_data_dir

        if self._orig_legacy_dir is None:
            os.environ.pop("MIMO_LEGACY_CONFIG_DIR", None)
        else:
            os.environ["MIMO_LEGACY_CONFIG_DIR"] = self._orig_legacy_dir

    def test_default_when_no_sources(self):
        cfg = load_config()
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_save_and_load_sqlite(self):
        cfg = load_config()
        cfg["cookie"] = "placeholder-cookie"
        cfg["opacity"] = 0.71
        cfg["position"] = [12, 34]
        cfg["always_on_top"] = False
        cfg["supergpt_base_url"] = "https://example.test/v1"
        cfg["supergpt_api_key"] = "test-supergpt-key"
        save_config(cfg)

        db_path = os.path.join(self.data_dir, "settings.db")
        self.assertTrue(os.path.exists(db_path))

        loaded = load_config()
        self.assertEqual(loaded["cookie"], "placeholder-cookie")
        self.assertAlmostEqual(loaded["opacity"], 0.71)
        self.assertEqual(loaded["position"], [12, 34])
        self.assertFalse(loaded["always_on_top"])
        self.assertEqual(loaded["supergpt_base_url"], "https://example.test/v1")
        self.assertNotIn("supergpt_api_key", loaded)

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT count(1) FROM settings").fetchone()[0]
        self.assertEqual(rows, len(DEFAULT_CONFIG))

        wal = db_path + "-wal"
        shm = db_path + "-shm"
        self.assertFalse(os.path.exists(wal))
        self.assertFalse(os.path.exists(shm))

    def test_migrate_from_legacy_json_when_db_empty(self):
        os.makedirs(self.legacy_dir, exist_ok=True)
        legacy_cfg = {"cookie": "legacy-placeholder", "opacity": 0.66}
        with open(os.path.join(self.legacy_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_cfg, f, ensure_ascii=False)

        cfg = load_config()
        self.assertEqual(cfg["cookie"], "legacy-placeholder")
        self.assertAlmostEqual(cfg["opacity"], 0.66)

        db_path = os.path.join(self.data_dir, "settings.db")
        self.assertTrue(os.path.exists(db_path))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_dir, "config.json")))

        cfg2 = load_config()
        self.assertEqual(cfg2["cookie"], "legacy-placeholder")

        with open(os.path.join(self.legacy_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"cookie": "stale-legacy-value"}, f, ensure_ascii=False)
        self.assertEqual(load_config()["cookie"], "legacy-placeholder")

    def test_corrupt_db_value_falls_back_to_legacy(self):
        os.makedirs(self.legacy_dir, exist_ok=True)
        with open(os.path.join(self.legacy_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"cookie": "legacy-after-corruption"}, f, ensure_ascii=False)

        db_path = os.path.join(self.data_dir, "settings.db")
        os.makedirs(self.data_dir, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO settings(key, value_json) VALUES (?, ?)",
                ("cookie", "not-json"),
            )

        cfg = load_config()
        self.assertEqual(cfg["cookie"], "legacy-after-corruption")

    def test_legacy_fallback_when_db_dir_unusable(self):
        os.makedirs(self.legacy_dir, exist_ok=True)
        legacy_cfg = {"cookie": "fallback-placeholder", "display_mode": "third_party"}
        with open(os.path.join(self.legacy_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_cfg, f, ensure_ascii=False)

        os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = os.path.join(self._tmp, "no-access", "x")

        cfg = load_config()
        self.assertEqual(cfg["cookie"], "fallback-placeholder")
        self.assertEqual(cfg["display_mode"], "third_party")

    def test_save_fallback_atomic_json_when_db_unusable(self):
        os.makedirs(self.legacy_dir, exist_ok=True)

        bad_db_dir = os.path.join(self._tmp, "blocked-db")
        os.makedirs(bad_db_dir, exist_ok=True)
        bad_db_path = os.path.join(bad_db_dir, "settings.db")
        with open(bad_db_path, "w", encoding="utf-8") as f:
            f.write("not-a-sqlite-db")

        os.environ["MIMO_TOKEN_MONITOR_DATA_DIR"] = bad_db_dir

        cfg = load_config()
        cfg["cookie"] = "fallback-save-placeholder"
        save_config(cfg)

        path = os.path.join(self.legacy_dir, "config.json")
        self.assertTrue(os.path.exists(path))

        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded["cookie"], "fallback-save-placeholder")
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_unknown_fields_preserved(self):
        cfg = load_config()
        cfg["cookie"] = "placeholder"
        cfg["unknown_field"] = 123
        save_config(cfg)

        loaded = load_config()
        self.assertEqual(loaded["unknown_field"], 123)

    def test_old_keys_migrate_once_without_losing_other_settings(self):
        os.makedirs(self.legacy_dir, exist_ok=True)
        legacy = {"cookie": "keep-cookie", "third_party_api_key": "current-wlb",
                  "supergpt_api_key": "current-supergpt", "unrelated": 42}
        with open(os.path.join(self.legacy_dir, "config.json"), "w", encoding="utf-8") as stream:
            json.dump(legacy, stream)
        cfg = load_config()
        self.assertEqual(cfg["third_party_api_key"], "current-wlb")
        with patch("config.save_keys") as save_keys, \
             patch("config.read_persistent_key", return_value=""), \
             patch("config.env_override", return_value=""):
            conflicts = migrate_provider_keys(cfg)
            migrate_provider_keys(load_config())
        self.assertEqual(conflicts, [])
        self.assertEqual(save_keys.call_count, 2)
        save_keys.assert_any_call({"wlb-relay": "current-wlb"}, enable=False)
        save_keys.assert_any_call({"supergpt-relay": "current-supergpt"}, enable=False)
        self.assertNotIn("third_party_api_key", cfg)
        self.assertNotIn("supergpt_api_key", cfg)
        self.assertEqual(load_config()["cookie"], "keep-cookie")
        self.assertEqual(load_config()["unrelated"], 42)
        with open(os.path.join(self.legacy_dir, "config.json"), encoding="utf-8") as stream:
            self.assertNotIn("third_party_api_key", json.load(stream))

    def test_failed_migration_preserves_old_settings_and_stops_cleanup(self):
        cfg = {"third_party_api_key": "keep-key", "cookie": "keep-cookie"}
        with patch("config.save_keys", side_effect=ProviderKeyError("写入失败")), \
             patch("config.read_persistent_key", return_value=""), \
             patch("config.env_override", return_value=""), \
             patch("config.save_config") as save:
            with self.assertRaises(ProviderKeyError) as raised:
                migrate_provider_keys(cfg)
        save.assert_not_called()
        self.assertEqual(cfg["third_party_api_key"], "keep-key")
        self.assertNotIn("keep-key", str(raised.exception))
        with patch("config.save_keys"), \
             patch("config.read_persistent_key", return_value=""), \
             patch("config.env_override", return_value=""), \
             patch("config.save_config", return_value=False):
            with self.assertRaisesRegex(ProviderKeyError, "清理失败"):
                migrate_provider_keys(cfg)
        self.assertEqual(cfg["third_party_api_key"], "keep-key")

    def test_migration_keeps_different_router_key_and_reports_conflict(self):
        cfg = {"third_party_api_key": "stale-key", "cookie": "keep"}
        with patch("config.save_keys") as save_keys, \
             patch("config.read_persistent_key", return_value="router-new"), \
             patch("config.env_override", return_value=""):
            conflicts = migrate_provider_keys(cfg)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["field"], "third_party_api_key")
            self.assertEqual(conflicts[0]["label"], "WLB")
            self.assertEqual(conflicts[0]["reason"], "different")
            # 再次运行仍然只是冲突：旧值永远不会自动覆盖新 Key。
            again = migrate_provider_keys(cfg)
        save_keys.assert_not_called()
        self.assertEqual(again, conflicts)
        self.assertEqual(cfg["third_party_api_key"], "stale-key")

    def test_migration_equal_value_cleans_without_write(self):
        cfg = {"supergpt_api_key": "same-key", "cookie": "keep"}
        with patch("config.save_keys") as save_keys, \
             patch("config.read_persistent_key", return_value="same-key"), \
             patch("config.env_override", return_value=""):
            self.assertEqual(migrate_provider_keys(cfg), [])
        save_keys.assert_not_called()
        self.assertNotIn("supergpt_api_key", cfg)

    def test_migration_env_override_same_and_different(self):
        cfg = {"third_party_api_key": "env-same", "cookie": "keep"}
        with patch("config.save_keys") as save_keys, \
             patch("config.env_override", return_value="env-same"), \
             patch("config.read_persistent_key", return_value=""):
            self.assertEqual(migrate_provider_keys(cfg), [])
        save_keys.assert_not_called()
        self.assertNotIn("third_party_api_key", cfg)

        cfg = {"third_party_api_key": "stale-key", "cookie": "keep"}
        with patch("config.save_keys") as save_keys, \
             patch("config.env_override", return_value="env-other"), \
             patch("config.read_persistent_key", return_value="persist-other"):
            conflicts = migrate_provider_keys(cfg)
        save_keys.assert_not_called()
        self.assertEqual(conflicts[0]["reason"], "env")
        self.assertEqual(cfg["third_party_api_key"], "stale-key")

    def test_partial_failure_per_item_status_and_idempotent_retry(self):
        cfg = {"third_party_api_key": "wlb-old", "supergpt_api_key": "supergpt-old",
               "cookie": "keep"}
        writes = []

        def save_side_effect(keys, *, enable=True):
            writes.append((dict(keys), enable))
            if "supergpt-relay" in keys:
                raise ProviderKeyError("SuperGPT 密钥保存失败")

        with patch("config.save_keys", side_effect=save_side_effect), \
             patch("config.read_persistent_key", return_value=""), \
             patch("config.env_override", return_value=""):
            with self.assertRaises(ProviderKeyError) as raised:
                migrate_provider_keys(cfg)
        self.assertIn("WLB 已完成", str(raised.exception))
        self.assertIn("SuperGPT", str(raised.exception))
        self.assertNotIn("wlb-old", str(raised.exception))
        self.assertNotIn("supergpt-old", str(raised.exception))
        self.assertNotIn("third_party_api_key", cfg)
        self.assertEqual(cfg["supergpt_api_key"], "supergpt-old")

        # 重试：Router 值已被用户更新时只报告冲突，旧值不写入（防二次覆盖）。
        writes.clear()

        def read_side_effect(provider):
            return "router-brand-new" if provider == "supergpt-relay" else ""

        with patch("config.save_keys",
                   side_effect=lambda keys, *, enable=True: writes.append((dict(keys), enable))), \
             patch("config.read_persistent_key", side_effect=read_side_effect), \
             patch("config.env_override", return_value=""):
            conflicts = migrate_provider_keys(cfg)
        self.assertEqual(writes, [])
        self.assertEqual(conflicts[0]["reason"], "different")
        self.assertEqual(cfg["supergpt_api_key"], "supergpt-old")

    def test_conflict_resolution_overwrite_and_keep(self):
        cfg = {"third_party_api_key": "stale-key", "cookie": "keep"}
        with patch("config.save_keys") as save_keys, \
             patch("config.read_persistent_key", return_value="router-new"), \
             patch("config.env_override", return_value=""):
            conflicts = migrate_provider_keys(cfg)
            resolve_key_migration(cfg, conflicts[0]["field"], overwrite=False)
        save_keys.assert_not_called()
        self.assertNotIn("third_party_api_key", cfg)

        cfg = {"third_party_api_key": "stale-key", "cookie": "keep"}
        with patch("config.save_keys") as save_keys, \
             patch("config.read_persistent_key", return_value="router-new"), \
             patch("config.env_override", return_value=""):
            conflicts = migrate_provider_keys(cfg)
            resolve_key_migration(cfg, conflicts[0]["field"], overwrite=True)
        save_keys.assert_called_once_with({"wlb-relay": "stale-key"}, enable=False)
        self.assertNotIn("third_party_api_key", cfg)

    def test_fallback_json_never_stores_provider_keys(self):
        with patch("config._open_db", return_value=None):
            self.assertTrue(save_config({"third_party_api_key": "private", "supergpt_api_key": "private"}))
        with open(os.path.join(self.legacy_dir, "config.json"), encoding="utf-8") as stream:
            cfg = json.load(stream)
        self.assertNotIn("third_party_api_key", cfg)
        self.assertNotIn("supergpt_api_key", cfg)


if __name__ == "__main__":
    unittest.main()
