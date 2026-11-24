from pathlib import Path

from checkcord.core.config import Settings

DOC = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"


def test_every_setting_is_documented() -> None:
    text = DOC.read_text()
    missing = [name.upper() for name in Settings.model_fields if f"`{name.upper()}`" not in text]
    assert missing == []
