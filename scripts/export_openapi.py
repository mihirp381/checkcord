"""Write the API's OpenAPI document to docs/openapi.json.

uv run python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def render() -> str:
    from checkcord.main import app

    return json.dumps(app.openapi(), indent=2) + "\n"


def main() -> int:
    PATH.write_text(render())
    print(f"wrote {PATH.relative_to(Path.cwd()) if PATH.is_relative_to(Path.cwd()) else PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
