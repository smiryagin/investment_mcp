import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PluginMetadataTests(unittest.TestCase):
    def test_openai_listing_uses_wiseline_branding(self) -> None:
        manifest = json.loads((REPOSITORY_ROOT / "plugin.json").read_text(encoding="utf-8"))
        interface = manifest["extensions"]["com.openai"]["interface"]

        self.assertEqual(interface["displayName"], "WiseLine Trade Investments")
        self.assertEqual(interface["developerName"], "WiseLine Trade")
        self.assertEqual(interface["category"], "Finance")
        self.assertEqual(interface["websiteURL"], "https://wiselinetrade.com")
        self.assertIn("Investment portfolio tools", interface["shortDescription"])

    def test_package_maps_the_registered_chatgpt_connection(self) -> None:
        plugin = json.loads((REPOSITORY_ROOT / "plugin.json").read_text(encoding="utf-8"))
        app_manifest = json.loads(
            (REPOSITORY_ROOT / ".app.json").read_text(encoding="utf-8")
        )
        app = app_manifest["apps"]["wiseline-investments"]

        self.assertEqual(
            plugin["extensions"]["com.openai"]["apps"],
            "./.app.json",
        )
        self.assertEqual(
            app["id"],
            "asdk_app_6ac7b51e8b048191a06b8d5b52cdfae6",
        )
        self.assertTrue(app["required"])


if __name__ == "__main__":
    unittest.main()
