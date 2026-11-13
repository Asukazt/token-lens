"""Run the installed core's self-tests with synthetic, offline price data."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "plugins" / "api-balance"))
import server

catalog = {
    "models": {
        "ark:deepseek-v4.1-flash-1017": {
            "provider": "ark",
            "input": 2.0,
            "cached": 0.1,
            "output": 8.0,
            "idle": {
                "enabled": True,
                "windows": [
                    {"start": "00:30", "end": "08:30"},
                    {"start": "12:00", "end": "14:00"},
                    {"start": "18:00", "end": "24:00"},
                ],
            },
        }
    }
}

if __name__ == "__main__":
    with patch.object(server, "_codex_provider", return_value={}), patch.object(
        server.pricing_catalog, "load", return_value=catalog
    ):
        sys.exit(server.selftest())
