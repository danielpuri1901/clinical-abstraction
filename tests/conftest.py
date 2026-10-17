"""Tests never call a model. Any path to Claude or Jev fails fast here; tests inject fakes instead."""

import pytest


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise RuntimeError("tests must not call a model; inject a fake call or transport")

    for target in ("clinical_abstraction.read.call_anthropic", "clinical_abstraction.read.default_transport"):
        monkeypatch.setattr(target, refuse)
