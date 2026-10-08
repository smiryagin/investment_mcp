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

    def test_packaged_mcp_server_uses_staging_oauth_endpoint(self) -> None:
        manifest = json.loads((REPOSITORY_ROOT / "mcp.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"]["wiseline-investments"]

        self.assertEqual(server["type"], "streamable-http")
        self.assertEqual(
            server["url"],
            "https://staging-investments-mcp.wiselinetrade.com/mcp",
        )


if __name__ == "__main__":
    unittest.main()
