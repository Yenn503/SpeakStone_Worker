"""Tests never reach GitHub, never open a tray icon and never swap a program: both switches are on for every test,
and a test that wants the real behaviour passes its own environment or fakes."""
import pytest


@pytest.fixture(autouse=True)
def _no_update_no_tray(monkeypatch):
    monkeypatch.setenv("SSW_NO_UPDATE", "1")
    monkeypatch.setenv("SSW_NO_TRAY", "1")
