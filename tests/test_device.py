"""Tests for config.embed_device(): which device the indexer embeds on.

    uv run --with pytest pytest tests
"""

import platform
import sys

import pytest
import torch

from findling import config as cfg


@pytest.mark.parametrize(
    "os_name, machine, mps, cuda, expected",
    [
        ("darwin", "arm64", True, False, "mps"),
        ("darwin", "arm64", False, False, "cpu"),
        ("darwin", "x86_64", True, False, "cpu"),  # Intel Mac with an AMD GPU
        ("linux", "x86_64", False, True, "cuda"),
        ("linux", "x86_64", False, False, "cpu"),
        ("linux", "aarch64", False, True, "cuda"),
    ],
)
def test_embed_device(monkeypatch, os_name, machine, mps, cuda, expected):
    monkeypatch.delenv("FINDLING_DEVICE", raising=False)
    monkeypatch.setattr(sys, "platform", os_name)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
    assert cfg.embed_device() == expected


def test_env_var_overrides(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setenv("FINDLING_DEVICE", "cpu")
    assert cfg.embed_device() == "cpu"
