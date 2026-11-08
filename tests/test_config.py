import pytest

from findling import config as cfg


def test_ocr_defaults_off_and_gets_listed(tmp_path):
    path = tmp_path / "folders.toml"
    path.write_text('folders = []\nextensions = ["juris"]  # keep me\n', encoding="utf-8")
    assert cfg.load_config(path).ocr is False
    cfg.ensure_settings_listed(path)
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text and "ocr = false" in text
    cfg.ensure_settings_listed(path)  # already there: unchanged
    assert path.read_text(encoding="utf-8") == text
    path.write_text(text.replace("ocr = false", "ocr = true"), encoding="utf-8")
    assert cfg.load_config(path).ocr is True


def test_ocr_creates_missing_file_and_rejects_non_bool(tmp_path):
    path = tmp_path / "sub" / "folders.toml"
    cfg.ensure_settings_listed(path)
    assert cfg.load_config(path).ocr is False
    path.write_text('ocr = "yes"\n', encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)
