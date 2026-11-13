import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server
import widget_v2 as widget


BASE = "https://api.example.test/v1"
MODEL = "profile-test-model"


def profile(name, multiplier=1.0):
    return {
        "name": name,
        "base_url": BASE,
        "multiplier": multiplier,
        "models": {MODEL: {"input": 2, "cached": 0.2, "output": 8}},
    }


def configuration():
    return {
        "relay": {"base_url": BASE, "api_key": "test-key", "inherit": False},
        "aliyun": {"access_key_id": "test-id", "access_key_secret": "test-secret"},
        "pricing": {
            "provider": "custom", "provider_base": BASE, "profile": "standard",
            "profiles": {"standard": profile("Standard"), "discount": profile("Discount", 0.5)},
            "models": {"other-model": {"input": 99, "cached": 99, "output": 99}},
            "multiplier": 99,
        },
    }


class ProfilePricingTests(unittest.TestCase):
    def setUp(self):
        self.provider_patch = patch.object(server, "_codex_provider", return_value={})
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def test_same_url_profiles_resolve_by_id_and_never_use_other_models(self):
        cfg = configuration()
        for selected, expected in (("standard", 1), ("discount", 0.5)):
            cfg["pricing"]["profile"] = selected
            pricing = server._price_config(copy.deepcopy(cfg))
            self.assertEqual(pricing["profile_key"], selected)
            self.assertEqual(pricing["multiplier"], expected)
            self.assertNotIn("other-model", pricing["models"])

    def test_legacy_domain_profile_is_kept_without_migration(self):
        cfg = configuration()
        cfg["pricing"]["profile"] = "api.example.test"
        cfg["pricing"]["profiles"] = {"api.example.test": profile("Legacy", 0.2)}
        self.assertEqual(server._price_config(cfg)["profile_key"], "api.example.test")
        self.assertEqual(cfg["pricing"]["profiles"]["api.example.test"]["name"], "Legacy")

    def test_api_key_bindings_follow_key_and_do_not_store_plaintext(self):
        cfg = configuration()
        pricing = cfg["pricing"]
        server._bind_pricing_profile(pricing, "standard", BASE, "test-key")
        server._bind_pricing_profile(pricing, "discount", BASE, "discount-key")
        self.assertNotIn("test-key", json.dumps(pricing))
        self.assertNotIn("discount-key", json.dumps(pricing))
        self.assertEqual(server._price_config(copy.deepcopy(cfg))["profile_key"], "standard")
        cfg["relay"]["api_key"] = "discount-key"
        self.assertEqual(server._price_config(copy.deepcopy(cfg))["profile_key"], "discount")
        cfg["relay"]["api_key"] = "unknown-key"
        resolved = server._price_config(cfg)
        self.assertTrue(resolved["profile_ambiguous"])
        self.assertFalse(resolved["models"])
        self.assertEqual(resolved["multiplier"], 1)
        self.assertEqual(server._price_for(MODEL, resolved), (None, "missing"))

    def test_manual_switch_with_same_key_updates_binding(self):
        cfg = configuration()
        server._bind_pricing_profile(cfg["pricing"], "standard", BASE, "test-key")
        server._bind_pricing_profile(cfg["pricing"], "discount", BASE, "test-key")
        self.assertEqual(server._price_config(cfg)["profile_key"], "discount")

    def test_different_host_never_uses_selected_profile_or_global_prices(self):
        cfg = configuration()
        cfg["relay"]["base_url"] = "https://other.example.test/v1"
        resolved = server._price_config(cfg)
        self.assertFalse(resolved["profile"])
        self.assertNotIn("other-model", resolved["models"])
        self.assertEqual(resolved["multiplier"], 1)

    def test_blank_profile_does_not_inherit_global_model_prices(self):
        cfg = configuration()
        cfg["pricing"]["profiles"]["standard"]["models"] = {}
        self.assertEqual(server._price_config(cfg)["models"], {})

    def test_snapshot_keeps_tiers_and_prices_when_usage_or_settings_change(self):
        cfg = configuration()
        entry = cfg["pricing"]["profiles"]["standard"]["models"][MODEL]
        entry["tiers"] = [
            {"upto": 1000000, "input": 2, "cached": 0.2, "output": 8},
            {"upto": 3000000, "input": 4, "cached": 0.4, "output": 16},
        ]
        pricing = server._price_config(cfg)
        turn = {"at": datetime.now(timezone.utc).isoformat(),
                "usage": {"input_tokens": 500000}}
        self.assertTrue(server._turn_pricing_snapshot(turn, MODEL, pricing))
        pricing["models"][MODEL]["tiers"][1]["input"] = 99
        turn["usage"]["input_tokens"] = 2000000
        self.assertFalse(server._turn_pricing_snapshot(turn, MODEL, pricing))
        self.assertEqual(server._estimate_cost(turn["usage"], MODEL, turn["pricing_snapshot"])[0], 8)
        self.assertNotIn("test-key", json.dumps(turn["pricing_snapshot"]))

    def test_missing_price_is_not_frozen_before_user_configures_model(self):
        cfg = configuration()
        cfg["pricing"]["profiles"]["standard"]["models"] = {}
        turn = {"usage": {"input_tokens": 100}}
        self.assertFalse(server._turn_pricing_snapshot(turn, MODEL, server._price_config(cfg)))
        self.assertNotIn("pricing_snapshot", turn)

    def test_key_cannot_be_bound_to_profile_of_another_host(self):
        pricing = configuration()["pricing"]
        original = copy.deepcopy(pricing)
        with self.assertRaises(ValueError):
            server._bind_pricing_profile(pricing, "standard", "https://other.test/v1", "test-key")
        self.assertEqual(pricing, original)

    def test_historical_turn_keeps_group_when_new_turn_uses_new_group(self):
        with tempfile.TemporaryDirectory() as directory:
            session = os.path.join(directory, "session.jsonl")
            cache = os.path.join(directory, "cache.json")
            stamp = datetime.now(timezone.utc).isoformat()
            records = [
                {"type": "session_meta", "timestamp": stamp,
                 "payload": {"id": "thread-1", "model": MODEL}},
                {"type": "token_usage_record", "timestamp": stamp,
                 "payload": {"turn_id": "turn-1", "usage": {"input_tokens": 1000000}}},
            ]
            with open(session, "w", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(record) + "\n" for record in records))
            cfg = configuration()
            first = server._price_config(copy.deepcopy(cfg))
            cfg["pricing"]["profile"] = "discount"
            second = server._price_config(copy.deepcopy(cfg))
            with patch.object(server, "SESSION_ROOT", directory), \
                    patch.object(server, "USAGE_CACHE_PATH", cache), \
                    patch.object(server, "_read_session_titles", return_value=({}, {})), \
                    patch.object(server, "_price_config", side_effect=[first, second]):
                original = server.usage_report()
                with open(session, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "type": "token_usage_record", "timestamp": stamp,
                        "payload": {"turn_id": "turn-2", "usage": {"input_tokens": 1000000}},
                    }) + "\n")
                switched = server.usage_report()
            self.assertEqual(original["recent_turns"][0]["cost"], 2)
            turns = {turn["turn_id"]: turn for turn in switched["recent_turns"]}
            self.assertEqual(turns["turn-1"]["cost"], 2)
            self.assertEqual(turns["turn-1"]["profile_name"], "Standard")
            self.assertEqual(turns["turn-2"]["cost"], 1)
            self.assertEqual(turns["turn-2"]["profile_name"], "Discount")


class ProfileSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "config.json")
        self.config_patch = patch.object(widget, "CONFIG_PATH", self.path)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.provider_patch = patch.object(server, "_codex_provider", return_value={})
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        widget.write_config(configuration())
        self.app = widget.Widget.__new__(widget.Widget)
        self.app.root = SimpleNamespace(after=Mock())
        self.app._render, self.app.refresh, self.app._clear_entries = Mock(), Mock(), Mock()
        self.app._entries = {}
        self.app._pending_base = ""
        self.app._provider_menu = True
        self.app._provider_page = 0
        self.app._idle_enabled_loaded_for = None
        self.app._hits = []
        self.app._rrect, self.app._text, self.app._line = Mock(), Mock(), Mock()
        self.app._fit_text = lambda text, *args, **kwargs: text

    def settings_inputs(self, **overrides):
        inputs = {
            "relay_base": BASE, "relay_key": "test-key", "profile_name": "Discount",
            "multiplier": "0.25", "price_input": "4", "price_cached": "0.4",
            "price_output": "16", "refresh_sec": "2", "balance_refresh_sec": "8",
        }
        inputs.update(overrides)
        self.app._entries = {
            key: SimpleNamespace(get=lambda value=value: value) for key, value in inputs.items()
        }
        self.app.idle_windows, self.app.idle_weekend, self.app.idle_enabled = [], False, False
        self.app.follow, self.app.texture, self.app.compact = False, True, True
        self.app.view, self.app.position, self.app.compact_pos = "settings", [20, 20], [20, 20]
        self.app._settings_values = lambda: (
            {"provider_key": "custom", "profile_key": "discount"}, MODEL
        )
        self.app.apply_config = Mock()

    def test_saving_one_group_does_not_change_other_group_or_global_prices(self):
        original = widget.read_config()
        self.settings_inputs()
        self.app.save_settings()
        saved = widget.read_config()
        self.assertEqual(saved["pricing"]["profile"], "discount")
        self.assertEqual(saved["pricing"]["profiles"]["standard"],
                         original["pricing"]["profiles"]["standard"])
        self.assertEqual(saved["pricing"]["models"], original["pricing"]["models"])
        self.assertEqual(saved["pricing"]["multiplier"], original["pricing"]["multiplier"])
        discount = saved["pricing"]["profiles"]["discount"]
        self.assertEqual(discount["multiplier"], 0.25)
        self.assertEqual(discount["models"][MODEL], {"input": 4, "cached": 0.4, "output": 16})
        self.assertEqual(saved["aliyun"], original["aliyun"])

    def test_idle_settings_are_independent_for_each_group(self):
        original = widget.read_config()
        self.settings_inputs(idle_input="1", idle_cached="0.1", idle_output="3")
        self.app.idle_windows = [["12:00", "14:00"]]
        self.app.idle_enabled, self.app.idle_weekend = True, True
        self.app.save_settings()
        saved = widget.read_config()
        idle = saved["pricing"]["profiles"]["discount"]["models"][MODEL]["idle"]
        self.assertTrue(idle["enabled"])
        self.assertEqual(idle["windows"], [{"start": "12:00", "end": "14:00"}])
        self.assertEqual(saved["pricing"]["profiles"]["standard"],
                         original["pricing"]["profiles"]["standard"])

    def test_invalid_price_does_not_modify_file(self):
        original = widget.read_config()
        for value in ("nan", "inf", "-1"):
            with self.subTest(value=value):
                self.settings_inputs(multiplier=value)
                self.app.save_settings()
                self.assertEqual(widget.read_config(), original)
                self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_cancelled_delete_keeps_file_and_bindings(self):
        original = widget.read_config()
        with patch.object(widget.messagebox, "askyesno", return_value=False):
            self.app._invoke("profile_delete:standard")
        self.assertEqual(widget.read_config(), original)
        self.app.refresh.assert_not_called()

    def test_zero_prices_are_saved(self):
        self.settings_inputs(price_input="0", price_cached="0", price_output="0")
        self.app.save_settings()
        prices = widget.read_config()["pricing"]["profiles"]["discount"]["models"][MODEL]
        self.assertEqual(prices, {"input": 0, "cached": 0, "output": 0})

    def test_both_new_entry_points_keep_existing_same_url_profiles(self):
        original = widget.read_config()
        for action in ("profile_new", "provider_pick:custom:new"):
            with self.subTest(action=action):
                widget.write_config(original)
                with patch.object(widget.simpledialog, "askstring", return_value="Another group"):
                    self.app._invoke(action)
                saved = widget.read_config()
                self.assertEqual(len(saved["pricing"]["profiles"]), 3)
                for key in ("standard", "discount"):
                    self.assertEqual(saved["pricing"]["profiles"][key],
                                     original["pricing"]["profiles"][key])
                created = saved["pricing"]["profiles"][saved["pricing"]["profile"]]
                self.assertEqual(created["base_url"], BASE)
                self.assertEqual(created["models"], {})
                self.assertEqual(created["multiplier"], 1)
                self.assertEqual(saved["aliyun"], original["aliyun"])
                self.assertEqual(saved["relay"], original["relay"])

    def test_copy_has_independent_id_models_idle_and_multiplier(self):
        original = widget.read_config()
        original["pricing"]["profiles"]["standard"]["models"][MODEL]["idle"] = {
            "enabled": True, "windows": [{"start": "12:00", "end": "14:00"}],
            "input": 1, "cached": 0.1, "output": 4,
        }
        widget.write_config(original)
        with patch.object(widget.simpledialog, "askstring", return_value="Copied group"):
            self.app._invoke("profile_copy:standard")
        saved = widget.read_config()
        key = saved["pricing"]["profile"]
        self.assertNotEqual(key, "standard")
        copied = saved["pricing"]["profiles"][key]
        self.assertEqual(copied["models"], original["pricing"]["profiles"]["standard"]["models"])
        self.assertEqual(copied["multiplier"], 1)
        self.assertEqual(copied["name"], "Copied group")

    def test_pick_does_not_change_credentials_and_selects_correct_group(self):
        original = widget.read_config()
        self.app._invoke("provider_pick:custom:discount")
        saved = widget.read_config()
        self.assertEqual(server._price_config(saved)["profile_key"], "discount")
        self.assertEqual(saved["relay"], original["relay"])
        self.assertEqual(saved["aliyun"], original["aliyun"])

    def test_switch_updates_price_badge_without_waiting_for_balance(self):
        self.app.snapshot = {"usage": {
            "current": {"model": MODEL},
            "pricing": {"current_source": "idle", "current_entry": {"idle": {"enabled": True}}},
        }}
        self.app._invoke("provider_pick:custom:discount")
        pricing = self.app.snapshot["usage"]["pricing"]
        self.assertEqual(pricing["profile_name"], "Discount")
        self.assertEqual(pricing["current_source"], "configured")
        self.assertNotIn("idle", pricing["current_entry"])

    def test_late_refresh_does_not_restore_old_group_badge(self):
        cfg = widget.read_config()
        server._bind_pricing_profile(cfg["pricing"], "discount", BASE, "test-key")
        widget.write_config(cfg)
        self.app._state = lambda: {}
        with patch.object(widget, "write_state"):
            self.app._apply({"usage": {
                "current": {"model": MODEL},
                "pricing": {"profile_name": "Standard", "multiplier": 1},
            }}, "")
        self.assertEqual(self.app.snapshot["usage"]["pricing"]["profile_name"], "Discount")

    def test_switch_idle_editor_loads_settings_from_selected_group(self):
        cfg = configuration()
        cfg["pricing"]["profiles"]["standard"]["models"][MODEL]["idle"] = {
            "enabled": True, "windows": [{"start": "12:00", "end": "14:00"}],
            "input": 1, "cached": 0.1, "output": 4,
        }
        widget.write_config(cfg)
        self.app._usage_parts = lambda: ({}, {}, {}, MODEL, 1)
        self.app.refresh_sec, self.app.balance_refresh_sec = 2, 8
        with patch.object(server, "relay_config", return_value=cfg["relay"]):
            self.app._settings_values()
            self.assertTrue(self.app.idle_enabled)
            self.assertEqual(self.app.idle_windows, [["12:00", "14:00"]])
            cfg["pricing"]["profile"] = "discount"
            widget.write_config(cfg)
            self.app._settings_values()
            self.assertFalse(self.app.idle_enabled)
            self.assertEqual(self.app.idle_windows, [])

    def test_delete_only_removes_target_and_never_selects_other_host(self):
        cfg = configuration()
        cfg["pricing"]["profiles"]["other"] = dict(profile("Other"),
                                                          base_url="https://other.test/v1")
        server._bind_pricing_profile(cfg["pricing"], "standard", BASE, "test-key")
        widget.write_config(cfg)
        with patch.object(widget.messagebox, "askyesno", return_value=True):
            self.app._invoke("profile_delete:standard")
            saved = widget.read_config()
            self.assertEqual(saved["pricing"]["profile"], "discount")
            self.app._invoke("profile_delete:discount")
        saved = widget.read_config()
        self.assertEqual(saved["relay"], cfg["relay"])
        self.assertEqual(saved["aliyun"], cfg["aliyun"])
        self.assertIn("other", saved["pricing"]["profiles"])
        self.assertFalse(saved["pricing"].get("profile"))
        self.assertNotIn("standard", (saved["pricing"].get("profile_bindings") or {}).values())

    def test_menu_pages_keep_every_action_inside_dialog(self):
        cfg = configuration()
        for index in range(14):
            cfg["pricing"]["profiles"]["group-%s" % index] = profile("Group %s" % index)
        widget.write_config(cfg)
        actions = set()
        for page in range(4):
            self.app._provider_page = page
            self.app._hits = []
            self.app._draw_provider_menu(None, widget.WIDTH, widget.HEIGHT)
            for x1, y1, x2, y2, action in self.app._hits:
                actions.add(action)
                if action.startswith(("provider_pick:", "profile_delete:", "profile_copy:")):
                    self.assertGreaterEqual(y1, 144)
                    self.assertLessEqual(y2, 400)
        for key in cfg["pricing"]["profiles"]:
            self.assertIn("provider_pick:custom:" + key, actions)
            self.assertIn("profile_delete:" + key, actions)
            self.assertIn("profile_copy:" + key, actions)
        self.assertIn("provider_pick:custom:new", actions)

    def test_write_error_is_reported_without_success_or_refresh(self):
        original = widget.read_config()
        with patch.object(widget.simpledialog, "askstring", return_value="Unsaved"), \
                patch.object(widget, "write_config", side_effect=OSError("test write error")):
            self.app._invoke("profile_new")
        self.assertEqual(widget.read_config(), original)
        self.assertEqual(self.app._settings_status_color, widget.BAD)
        self.app.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
