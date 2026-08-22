import pathlib
import unittest


PANEL_SOURCE = (pathlib.Path(__file__).parents[1] / "Panel.qml").read_text()


class PanelStateTests(unittest.TestCase):
    def test_periodic_status_poll_does_not_dim_hvac_controls(self):
        start = PANEL_SOURCE.index("readonly property bool hvacControlsEnabled:")
        end = PANEL_SOURCE.index("readonly property var iconTempModes", start)

        self.assertNotIn("statusProc.running", PANEL_SOURCE[start:end])

    def test_periodic_status_poll_does_not_dim_discovery_button(self):
        start = PANEL_SOURCE.index('text: "Discover zones"')
        enabled = PANEL_SOURCE.rfind("enabled:", 0, start)

        self.assertNotIn("statusProc.running", PANEL_SOURCE[enabled:start])


if __name__ == "__main__":
    unittest.main()
