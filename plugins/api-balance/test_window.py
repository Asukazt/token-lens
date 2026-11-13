import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import widget_v2 as widget


class WindowTests(unittest.TestCase):
    def test_small_main_window_is_recognized_but_helpers_are_not(self):
        for case, expected in (
            ("small", 100), ("owned", None), ("tool", None),
            ("untitled", None), ("minimized", None),
        ):
            with self.subTest(case=case):
                api = Mock()
                api.IsWindowVisible.return_value = True
                api.IsIconic.return_value = case == "minimized"
                api.GetWindow.return_value = 200 if case == "owned" else 0
                api.GetWindowLongPtrW.return_value = 0x80 if case == "tool" else 0
                api.GetWindowTextLengthW.return_value = 0 if case == "untitled" else 7
                api.GetWindowThreadProcessId.side_effect = (
                    lambda hwnd, pid: setattr(pid._obj, "value", 123)
                )
                api.EnumWindows.side_effect = lambda callback, _: callback(100, 0)
                rect = (0, 0, 620, 360) if case == "small" else (0, 0, 1000, 800)
                with patch.object(widget, "user32", api), \
                        patch.object(widget, "_rect", return_value=rect), \
                        patch.object(widget, "_process_name", return_value="ChatGPT.exe"):
                    self.assertEqual(widget.find_host_hwnd(), expected)

    def test_tick_keeps_live_host_when_scan_temporarily_misses_it(self):
        app = self.make_widget()
        with patch.object(widget, "host_alive", return_value=True), \
                patch.object(widget, "find_host_hwnd", return_value=None), \
                patch.object(widget, "_rect", return_value=(0, 0, 1000, 800)), \
                patch.object(widget, "_foreground_pid", return_value=900), \
                patch.object(widget, "log_exc") as errors:
            app.tick()
        errors.assert_not_called()
        app._hide.assert_not_called()
        app._show_at.assert_called_once_with(20, 20)
        self.assertEqual(app._host_hwnd, 100)

    def test_other_app_focus_does_not_stop_host_layer_correction(self):
        app = self.make_widget()
        with patch.object(widget, "host_alive", return_value=True), \
                patch.object(widget, "find_host_hwnd", return_value=100), \
                patch.object(widget, "_rect", return_value=(0, 0, 1000, 800)), \
                patch.object(widget, "_foreground_pid", return_value=900), \
                patch.object(widget, "log_exc") as errors:
            app.tick()
        errors.assert_not_called()
        app._place_above_host.assert_called_once_with(100)

    def test_missing_or_minimized_host_hides_immediately(self):
        app = self.make_widget()
        with patch.object(widget, "host_alive", return_value=False), \
                patch.object(widget, "find_host_hwnd", return_value=None), \
                patch.object(widget, "log_exc") as errors:
            app.tick()
        errors.assert_not_called()
        app._hide.assert_called_once_with()
        app._show_at.assert_not_called()

    def test_compact_clamp_does_not_overwrite_saved_position(self):
        app = self.make_widget()
        app.position = [250, 20]
        app.compact_pos = [250, 20]
        app.size = (widget.COMPACT_WIDTH, widget.COMPACT_HEIGHT)
        app._clamp = widget.Widget._clamp.__get__(app, widget.Widget)
        with patch.object(widget, "host_alive", return_value=True), \
                patch.object(widget, "find_host_hwnd", return_value=100), \
                patch.object(widget, "_rect", return_value=(0, 0, 300, 300)), \
                patch.object(widget, "_foreground_pid", return_value=900):
            app.tick()
        self.assertEqual(app.position, [250, 20])
        self.assertEqual(app.compact_pos, [250, 20])
        app._show_at.assert_called_once_with(175, 20)

    def test_layer_correction_stays_below_other_apps_without_activating(self):
        app = widget.Widget.__new__(widget.Widget)
        app._host_hwnd, app.visible = 100, True
        app.root = SimpleNamespace(winfo_id=lambda: 400)
        api = Mock()
        api.GetAncestor.return_value = 400
        api.GetWindow.side_effect = lambda hwnd, _: {100: 200, 200: 300}[hwnd]
        api.GetWindowLongPtrW.side_effect = lambda hwnd, _: 8 if hwnd == 200 else 0
        with patch.object(widget, "user32", api):
            app._place_above_host()
        api.SetWindowPos.assert_called_once_with(400, 300, 0, 0, 0, 0, 0x13)

    @staticmethod
    def make_widget():
        app = widget.Widget.__new__(widget.Widget)
        app.plugin_config = None
        app._host_hwnd, app._host_scan_at = 100, 0
        app._host_origin, app.follow, app._dragging = (0, 0), False, False
        app.position, app.compact, app.visible = [20, 20], True, True
        app._front_pid, app._front_name, app._last_z_order = 900, "other.exe", 0
        app._tick_count, app._tick_window = 0, widget.time.monotonic()
        app._clamp = lambda x, y, rect: (x, y)
        app._hide, app._show_at, app._place_above_host = Mock(), Mock(), Mock()
        app._set_timer_resolution = Mock()
        app.root = SimpleNamespace(after=Mock())
        return app


if __name__ == "__main__":
    unittest.main()
