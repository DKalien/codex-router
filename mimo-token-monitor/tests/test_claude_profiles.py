# encoding: utf-8
import json
import os
import pathlib
import tempfile
import unittest

_root = pathlib.Path(__file__).resolve().parent.parent
if str(_root) not in os.sys.path:
    os.sys.path.insert(0, str(_root))

from claude_profiles import (  # noqa: E402
    ClaudeProfileError,
    PROFILE_ENV_KEYS,
    apply_profile,
    current_profile_name,
    load_profiles,
    save_profiles,
)


class TestClaudeProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claude_profiles_test_")
        self.settings_path = pathlib.Path(self.tmp.name) / "settings.json"
        self.profiles_path = pathlib.Path(self.tmp.name) / "profiles.json"
        self.env = {
            key: f"current-{key.lower()}" for key in PROFILE_ENV_KEYS
        }
        self.original_settings = {
            "env": {**self.env, "KEEP_ME": "untouched"},
            "permissions": {"defaultMode": "auto"},
        }
        self.settings_path.write_text(
            json.dumps(self.original_settings, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_load_bootstraps_current_settings_as_api_claude(self):
        profiles = load_profiles(self.settings_path, self.profiles_path)

        self.assertEqual([profile["name"] for profile in profiles], ["API-Claude"])
        self.assertEqual(profiles[0]["env"], self.env)
        self.assertTrue(self.profiles_path.exists())
        self.assertEqual(current_profile_name(profiles, self.settings_path), "API-Claude")

    def test_apply_changes_only_profile_env_keys(self):
        profiles = load_profiles(self.settings_path, self.profiles_path)
        replacement = {
            "name": "API-GPT",
            "env": {key: f"new-{key}" for key in PROFILE_ENV_KEYS},
        }

        apply_profile(replacement, self.settings_path)
        updated = json.loads(self.settings_path.read_text(encoding="utf-8"))

        self.assertEqual(updated["permissions"], self.original_settings["permissions"])
        self.assertEqual(updated["env"]["KEEP_ME"], "untouched")
        self.assertEqual(
            {key: updated["env"][key] for key in PROFILE_ENV_KEYS},
            replacement["env"],
        )
        self.assertIsNone(current_profile_name(profiles, self.settings_path))

    def test_invalid_profile_is_rejected_before_settings_write(self):
        original = self.settings_path.read_text(encoding="utf-8")
        invalid = {"name": "bad", "env": {PROFILE_ENV_KEYS[0]: "only-one"}}

        with self.assertRaises(ClaudeProfileError):
            apply_profile(invalid, self.settings_path)

        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), original)

    def test_save_profiles_supports_multiple_templates(self):
        profiles = [
            {
                "name": "API-Claude",
                "env": self.env,
            },
            {
                "name": "Mimo",
                "env": {key: f"mimo-{key}" for key in PROFILE_ENV_KEYS},
            },
        ]

        save_profiles(profiles, self.profiles_path)
        loaded = load_profiles(self.settings_path, self.profiles_path)

        self.assertEqual([profile["name"] for profile in loaded], ["API-Claude", "Mimo"])
        self.assertEqual(loaded[1]["env"]["ANTHROPIC_BASE_URL"], "mimo-ANTHROPIC_BASE_URL")


if __name__ == "__main__":
    unittest.main()
