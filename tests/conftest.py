"""Fixtures every streamstudio test shares."""

from typing import List

import pytest
from recordstream.sources import huggingface

from streamstudio import app as app_module


@pytest.fixture(autouse=True)
def page_checks(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """No test checks — let alone builds — the checkout's OWN editor page.

    A server start checks whether the page's source changed since its last build and runs npm when it did
    (``streamstudio.app.ensure_page_built``). A test that drives the CLI's start would run that check against the
    real ``frontend/``; here it is recorded instead (``"checked"``), so a test can still pin WHEN a start checks.
    The check itself is tested over a folder of its own (``tests/test_page_build.py``, fake ``npm``/``node``).
    """
    calls: List[str] = []
    monkeypatch.setattr(app_module, "ensure_page_built", lambda frontend=None: calls.append("checked"))
    return calls


@pytest.fixture(autouse=True)
def no_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test asks the HuggingFace Hub for a dataset's description.

    A HuggingFace source card's record-entry outputs are the source's ``produces``, which — with every metadata
    column kept (``"*"``, the default) — reads the dataset's description through
    ``recordstream.sources.huggingface.described_columns``: a network call on every derive of a graph holding such a
    source. Here it answers ``()``, what it answers offline (the source then declares ``image`` and ``class`` only); a
    test that needs a dataset's columns patches it again.
    """
    monkeypatch.setattr(huggingface, "described_columns", lambda path, name=None, revision=None: ())
