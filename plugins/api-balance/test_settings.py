import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import widget_v2 as widget


class SettingsTests(unittest.TestCase):
    def test_saving_other_platform_keeps_custom_api_in_menu(self):
        for provider in ("", "dashscope", "custom"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "config.json")
                profile = {
                    "name": "Saved API",
                    "base_url": "https://api.example.com/v1",
                    "multiplier": 0.15,
                    "models": {"saved-model": {"input": 2, "cached": 0.2, "output": 8}},
                }
                config = {
                    "relay": {"base_url": profile["base_url"], "api_key": "test-token"},
                    "aliyun": {"access_key_id": "test-id", "access_key_secret": "test-secret"},
                    "pricing": {
                        "provider": provider,
                        "profile": "api.example.com" if provider == "custom" else "",
                        "profiles": {"api.example.com": copy.deepcopy(profile)},
                        "multiplier": 0.15,
                        "models": {},
                    },
                }
                app = widget.Widget.__new__(widget.Widget)
                inputs = {
                    "relay_base": profile["base_url"], "relay_key": "test-token",
                    "multiplier": "0.15", "price_input": "2", "price_cached": "0.2",
                    "price_output": "8", "refresh_sec": "2", "balance_refresh_sec": "8",
                    "profile_name": "Saved API",
                }
                app._entries = {
                    key: SimpleNamespace(get=lambda value=value: value)
                    for key, value in inputs.items()
                }
                app.idle_windows, app.idle_weekend, app.idle_enabled = [], False, False
                app.follow, app.texture, app.compact, app.view = False, True, False, "settings"
                app.position, app.compact_pos = [20, 20], [30, 20]
                app.root = SimpleNamespace(after=Mock())
                app._settings_values = lambda: ({"provider_key": provider}, "current-model")
                app.apply_config = Mock()
                app._hits = []
                app._rrect, app._text, app._line = Mock(), Mock(), Mock()
                app._fit_text = lambda text, *args: text
                with patch.object(widget, "CONFIG_PATH", path):
                    widget.write_config(config)
                    app.save_settings()
                    saved = widget.read_config()
                    self.assertIn("api.example.com", saved["pricing"].get("profiles", {}))
                    self.assertEqual(saved["pricing"]["profiles"]["api.example.com"]["name"],
                                     "Saved API")
                    self.assertEqual(saved["pricing"]["profiles"]["api.example.com"]["models"]
                                     ["saved-model"], profile["models"]["saved-model"])
                    self.assertEqual(saved["aliyun"], config["aliyun"])
                    self.assertEqual(saved["relay"]["api_key"], "test-token")
                    app._draw_provider_menu(None, widget.WIDTH, widget.HEIGHT)
                    actions = [hit[-1] for hit in app._hits]
                    self.assertIn("provider_pick:custom:api.example.com", actions)
                    self.assertIn("profile_delete:api.example.com", actions)


if __name__ == "__main__":
    unittest.main()
