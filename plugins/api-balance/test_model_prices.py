import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import server
import widget_v2 as widget
from test_pricing_profiles import BASE, MODEL, configuration


class ModelPriceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config_patch = patch.object(widget, "CONFIG_PATH", os.path.join(directory.name, "config.json"))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        provider_patch = patch.object(server, "_codex_provider", return_value={})
        provider_patch.start()
        self.addCleanup(provider_patch.stop)
        widget.write_config(configuration())
        self.app = widget.Widget.__new__(widget.Widget)
        self.app.root = SimpleNamespace(after=Mock())
        self.app._render, self.app.refresh, self.app._clear_entries = Mock(), Mock(), Mock()
        self.app._entries = {}
        self.app._model_prices = None
        self.app._provider_menu = False
        self.app._settings_status = ""
        self.app._settings_status_color = widget.MUTED
        self.app._hits = []
        self.app.compact = False
        self.app._rrect, self.app._line, self.app._text = Mock(), Mock(), Mock()
        self.app._fit_text = lambda text, *args, **kwargs: str(text)

    def edit(self, profile="standard", **overrides):
        self.app._model_prices = {"profile_id": profile, "page": 0, "model": MODEL}
        values = {"model_input": "4", "model_cached": "0.4", "model_output": "16"}
        values.update(overrides)
        self.app._entries = {
            key: SimpleNamespace(get=lambda value=value: value) for key, value in values.items()
        }

    def test_open_pins_current_custom_group(self):
        self.app._invoke("model_prices")
        self.assertEqual(self.app._model_prices["profile_id"], "standard")
        self.assertEqual(self.app._model_prices["model"], "")

    def test_official_platform_has_no_model_manager(self):
        cfg = configuration()
        cfg["relay"]["base_url"] = "https://api.deepseek.com/v1"
        cfg["pricing"].update(provider="deepseek", provider_base=cfg["relay"]["base_url"])
        widget.write_config(cfg)
        self.app._invoke("model_prices")
        self.assertIsNone(self.app._model_prices)

    def test_preview_only_includes_models_saved_in_this_group(self):
        cfg = configuration()
        cfg["pricing"]["profiles"]["discount"]["models"]["discount-only"] = {
            "input": 1, "cached": 0.1, "output": 2,
        }
        widget.write_config(cfg)
        self.app._invoke("model_prices")
        self.app._draw_model_prices(None, widget.WIDTH, widget.HEIGHT)
        actions = [hit[-1] for hit in self.app._hits]
        self.assertIn("model_price_edit:" + MODEL, actions)
        self.assertNotIn("model_price_edit:discount-only", actions)
        self.assertNotIn("model_price_edit:other-model", actions)
        text = [str(call.args[3]) for call in self.app._text.call_args_list]
        self.assertIn("输入 2", text)
        self.assertIn("缓存 0.2", text)
        self.assertIn("输出 8", text)

    def test_saved_price_changes_only_target_model_in_target_group(self):
        original = widget.read_config()
        self.edit()
        self.app._invoke("model_price_save")
        saved = widget.read_config()
        expected = copy.deepcopy(original)
        expected["pricing"]["profiles"]["standard"]["models"][MODEL].update(
            input=4, cached=0.4, output=16,
        )
        self.assertEqual(saved, expected)
        self.assertEqual(self.app._model_prices["model"], "")

    def test_background_group_switch_does_not_redirect_edit(self):
        self.edit()
        cfg = widget.read_config()
        cfg["pricing"]["profile"] = "discount"
        widget.write_config(cfg)
        self.app._invoke("model_price_save")
        saved = widget.read_config()
        self.assertEqual(saved["pricing"]["profile"], "discount")
        self.assertEqual(saved["pricing"]["profiles"]["discount"],
                         cfg["pricing"]["profiles"]["discount"])
        self.assertEqual(saved["pricing"]["profiles"]["standard"]["models"][MODEL]["input"], 4)

    def test_idle_prices_change_without_losing_schedule_or_enabled_state(self):
        cfg = widget.read_config()
        idle = {"enabled": False, "windows": [{"start": "12:00", "end": "14:00"}],
                "weekend_all_day": True, "input": 1, "cached": 0.1, "output": 4}
        cfg["pricing"]["profiles"]["standard"]["models"][MODEL]["idle"] = idle
        widget.write_config(cfg)
        self.edit(model_idle_input="2", model_idle_cached="0.2", model_idle_output="6")
        self.app._invoke("model_price_save")
        actual = widget.read_config()["pricing"]["profiles"]["standard"]["models"][MODEL]["idle"]
        self.assertEqual(actual, dict(idle, input=2, cached=0.2, output=6))

    def test_invalid_values_never_write_configuration(self):
        original = widget.read_config()
        for value in ("", "-1", "nan", "inf", "not a number"):
            with self.subTest(value=value):
                self.edit(model_input=value)
                self.app._invoke("model_price_save")
                self.assertEqual(widget.read_config(), original)
                self.assertEqual(self.app._model_prices["model"], MODEL)
                self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_zero_prices_are_valid(self):
        self.edit(model_input="0", model_cached="0", model_output="0")
        self.app._invoke("model_price_save")
        model = widget.read_config()["pricing"]["profiles"]["standard"]["models"][MODEL]
        self.assertEqual(model, {"input": 0, "cached": 0, "output": 0})

    def test_deleted_profile_is_not_recreated_by_save(self):
        self.edit()
        cfg = widget.read_config()
        cfg["pricing"]["profiles"].pop("standard")
        widget.write_config(cfg)
        self.app._invoke("model_price_save")
        self.assertEqual(widget.read_config(), cfg)
        self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_deleted_model_is_not_recreated_by_save(self):
        self.edit()
        cfg = widget.read_config()
        cfg["pricing"]["profiles"]["standard"]["models"].pop(MODEL)
        widget.write_config(cfg)
        self.app._invoke("model_price_save")
        self.assertEqual(widget.read_config(), cfg)
        self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_write_failure_keeps_editor_open_and_reports_error(self):
        self.edit()
        original = widget.read_config()
        with patch.object(widget, "write_config", side_effect=OSError("test write error")):
            self.app._invoke("model_price_save")
        self.assertEqual(widget.read_config(), original)
        self.assertEqual(self.app._model_prices["model"], MODEL)
        self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_return_from_editor_and_list_does_not_write(self):
        self.edit()
        original = widget.read_config()
        self.app._invoke("model_prices_back")
        self.assertEqual(self.app._model_prices["model"], "")
        self.app._invoke("model_prices_back")
        self.assertIsNone(self.app._model_prices)
        self.assertEqual(widget.read_config(), original)

    def test_paging_keeps_all_models_reachable_inside_window(self):
        cfg = widget.read_config()
        for index in range(20):
            cfg["pricing"]["profiles"]["standard"]["models"]["model-%02d" % index] = {
                "input": index, "cached": 0, "output": index * 2,
            }
        widget.write_config(cfg)
        self.app._invoke("model_prices")
        actions = set()
        for page in range(4):
            self.app._model_prices["page"] = page
            self.app._hits = []
            self.app._draw_model_prices(None, widget.WIDTH, widget.HEIGHT)
            for x1, y1, x2, y2, action in self.app._hits:
                actions.add(action)
                if action.startswith("model_price_edit:"):
                    self.assertGreaterEqual(y1, 164)
                    self.assertLessEqual(y2, 554)
        for model in cfg["pricing"]["profiles"]["standard"]["models"]:
            self.assertIn("model_price_edit:" + model, actions)

    def test_editor_entries_do_not_reset_while_typing_during_refresh(self):
        self.edit()
        self.app._entries_key = ("model_prices", "standard", MODEL)
        self.app._entry = Mock()
        self.app._sync_model_price_entries()
        self.app._entry.assert_not_called()
        self.app._clear_entries.assert_not_called()

    def test_editor_has_separate_delete_and_save_targets(self):
        self.edit()
        self.app._draw_model_prices(None, widget.WIDTH, widget.HEIGHT)
        targets = {hit[-1]: hit[:4] for hit in self.app._hits}
        self.assertIn("model_price_delete", targets)
        self.assertLess(targets["model_price_save"][3], targets["model_price_delete"][1])
        self.assertLess(targets["model_price_delete"][3], widget.HEIGHT - 54)

    def test_confirmed_delete_removes_only_target_group_model(self):
        original = widget.read_config()
        original["pricing"]["profiles"]["standard"]["models"]["keep-model"] = {
            "input": 3, "cached": 0.3, "output": 9,
        }
        original["pricing"]["profiles"]["standard"]["models"][MODEL]["idle"] = {
            "enabled": True, "input": 1, "cached": 0.1, "output": 4,
        }
        widget.write_config(original)
        self.edit()
        with patch.object(widget.messagebox, "askyesno", return_value=True):
            self.app._invoke("model_price_delete")
        expected = copy.deepcopy(original)
        del expected["pricing"]["profiles"]["standard"]["models"][MODEL]
        self.assertEqual(widget.read_config(), expected)
        self.assertEqual(self.app._model_prices["model"], "")

    def test_cancelled_model_delete_keeps_prices_and_editor_values(self):
        original = widget.read_config()
        self.edit(model_input="7.25")
        with patch.object(widget.messagebox, "askyesno", return_value=False):
            self.app._invoke("model_price_delete")
        self.assertEqual(widget.read_config(), original)
        self.assertEqual(self.app._model_prices["model"], MODEL)
        self.assertEqual(self.app._entry_value("model_input"), "7.25")

    def test_delete_keeps_changes_made_during_confirmation_and_pins_group(self):
        self.edit()
        cfg = widget.read_config()
        cfg["pricing"]["profile"] = "discount"
        cfg["pricing"]["profiles"]["standard"]["models"]["new-model"] = {
            "input": 5, "cached": 0.5, "output": 10,
        }

        def confirm(*args, **kwargs):
            widget.write_config(cfg)
            return True

        with patch.object(widget.messagebox, "askyesno", side_effect=confirm):
            self.app._invoke("model_price_delete")
        expected = copy.deepcopy(cfg)
        del expected["pricing"]["profiles"]["standard"]["models"][MODEL]
        self.assertEqual(widget.read_config(), expected)

    def test_delete_handles_group_or_model_removed_during_confirmation(self):
        for remove_group in (False, True):
            with self.subTest(remove_group=remove_group):
                widget.write_config(configuration())
                self.edit()
                cfg = widget.read_config()
                if remove_group:
                    del cfg["pricing"]["profiles"]["standard"]
                else:
                    del cfg["pricing"]["profiles"]["standard"]["models"][MODEL]

                def confirm(*args, **kwargs):
                    widget.write_config(cfg)
                    return True

                with patch.object(widget.messagebox, "askyesno", side_effect=confirm):
                    self.app._invoke("model_price_delete")
                self.assertEqual(widget.read_config(), cfg)
                self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_delete_write_failure_preserves_price_and_editor(self):
        original = widget.read_config()
        self.edit()
        with patch.object(widget.messagebox, "askyesno", return_value=True), \
                patch.object(widget, "write_config", side_effect=OSError("test write error")):
            self.app._invoke("model_price_delete")
        self.assertEqual(widget.read_config(), original)
        self.assertEqual(self.app._model_prices["model"], MODEL)
        self.assertEqual(self.app._settings_status_color, widget.BAD)

    def test_custom_settings_show_current_model_after_switch_without_covering_entries(self):
        app = self.app
        app.refresh_sec, app.balance_refresh_sec = 2, 8
        app._idle_enabled_loaded_for = None
        app.follow, app.texture, app.view = False, False, "settings"
        app.idle_editor_open = False
        app._entry, app._draw_mini_switch, app._draw_switch_row = Mock(), Mock(), Mock()
        app.snapshot = {"usage": {"current": {"model": MODEL}}}
        for model in (MODEL, "switched-model"):
            app.snapshot["usage"]["current"]["model"] = model
            app._text.reset_mock()
            app._draw_settings(None, widget.WIDTH, widget.HEIGHT)
            model_calls = [call for call in app._text.call_args_list
                           if model in str(call.args[3])]
            self.assertTrue(model_calls, "Current model is missing in custom settings")
            app._sync_entries()
            model_y = model_calls[0].args[2]
            self.assertGreater(model_y, 188)
            price_entry = next(call for call in app._entry.call_args_list
                               if call.args[0] == "price_input")
            self.assertLess(model_y + 8, price_entry.args[2] - 10)

    def test_model_list_follows_current_group_with_different_models_at_same_url(self):
        self.app._invoke("model_prices")
        self.app._model_prices["page"] = 3
        cfg = widget.read_config()
        cfg["pricing"]["profile"] = "discount"
        cfg["pricing"]["profiles"]["discount"]["models"] = {
            "discount-only": {"input": 1, "cached": 0.1, "output": 3},
        }
        widget.write_config(cfg)
        self.app._draw_model_prices(None, widget.WIDTH, widget.HEIGHT)
        actions = {hit[-1] for hit in self.app._hits if hit[-1].startswith("model_price_edit:")}
        self.assertEqual(actions, {"model_price_edit:discount-only"})
        self.assertEqual(self.app._model_prices["profile_id"], "discount")
        self.assertEqual(self.app._model_prices["page"], 0)

    def test_open_editor_stays_in_original_group_when_current_group_changes(self):
        self.edit()
        cfg = widget.read_config()
        cfg["pricing"]["profile"] = "discount"
        widget.write_config(cfg)
        self.app._draw_model_prices(None, widget.WIDTH, widget.HEIGHT)
        self.assertEqual(self.app._model_prices["profile_id"], "standard")
        text = [str(call.args[3]) for call in self.app._text.call_args_list]
        self.assertIn("Standard", text)
        self.assertNotIn("Discount", text)

    def test_model_list_does_not_show_previous_group_when_selection_is_ambiguous(self):
        self.app._invoke("model_prices")
        cfg = widget.read_config()
        cfg["pricing"]["profile"] = ""
        widget.write_config(cfg)
        self.app._draw_model_prices(None, widget.WIDTH, widget.HEIGHT)
        actions = [hit[-1] for hit in self.app._hits if hit[-1].startswith("model_price_edit:")]
        self.assertEqual(actions, [])


class CompactLegibilityTests(unittest.TestCase):
    def make_app(self, group, multiplier=0.2):
        app = widget.Widget.__new__(widget.Widget)
        app.compact, app._hits = True, []
        app._balance = lambda: {"symbol": "$", "total": 5.48}
        app.snapshot = {"usage": {"pricing": {
            "profile_name": group, "multiplier": multiplier, "current_source": "configured",
        }}}
        return app

    def test_group_and_multiplier_use_readable_fixed_fonts_without_resizing(self):
        app = self.make_app("A very long group name that needs truncation")
        app._text = Mock()
        image = Image.new("RGB", (236, 60), widget.INK)
        app._draw_compact(ImageDraw.Draw(image), 118, 30)
        calls = app._text.call_args_list
        group = next(call for call in calls if str(call.args[3]).startswith("A very"))
        multiplier = next(call for call in calls if str(call.args[3]).startswith("×"))
        self.assertGreaterEqual(group.args[4], 8.5)
        self.assertGreaterEqual(multiplier.args[4], 8.5)
        self.assertNotEqual(group.args[5], widget.MUTED)
        self.assertTrue(str(group.args[3]).endswith("…"))
        self.assertEqual((widget.COMPACT_WIDTH, widget.COMPACT_HEIGHT), (118, 30))

    def test_compact_text_never_overlaps_or_leaves_window(self):
        for group in ("", "Saved Group", "A very long platform and group name"):
            for multiplier in (0, 0.2, 1, 100, 0.000001):
                with self.subTest(group=group, multiplier=multiplier):
                    app = self.make_app(group, multiplier)
                    image = Image.new("RGB", (236, 60), widget.INK)
                    draw = ImageDraw.Draw(image)
                    app._text = Mock()
                    app._draw_compact(draw, 118, 30)
                    bounds = []
                    for call in app._text.call_args_list:
                        _, x, y, text, size, *_ = call.args
                        options = call.kwargs
                        font = widget._font(size * widget.SCALE,
                                            bold=options.get("bold", False),
                                            numeric=options.get("numeric", False))
                        box = draw.textbbox((int(x * 2), int(y * 2)), text,
                                            font=font, anchor=options.get("anchor", "lm"))
                        self.assertGreaterEqual(box[0], 2)
                        self.assertGreaterEqual(box[1], 0)
                        self.assertLessEqual(box[2], 234)
                        self.assertLessEqual(box[3], 60)
                        for other in bounds:
                            self.assertFalse(box[0] < other[2] and box[2] > other[0]
                                             and box[1] < other[3] and box[3] > other[1])
                        bounds.append(box)


if __name__ == "__main__":
    unittest.main()
