"""Fixtures every streamstudio test shares."""

from pathlib import Path
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


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The per-user state folder (``~/.streamstudio``: the registry of running editors, the folder served last, the
    default templates folder) is a temporary folder of the test's own — never the real one, and not inside
    ``tmp_path``, which many tests list.

    ``Path.home`` is replaced rather than ``HOME``: the browser driver the page tests start reads ``HOME`` to find its
    browsers. A test that starts a real editor process hands that process a ``HOME`` of its own.
    """
    folder = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: folder))
    return folder


@pytest.fixture(autouse=True)
def _own_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test logs under its own tmp dir — never the operator's shared ``$LOGGAIR_DIR``."""
    monkeypatch.setenv("LOGGAIR_DIR", str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def browser_opens(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """No test opens a tab in the operator's browser: the addresses a start would open are recorded instead."""
    import webbrowser

    opened: List[str] = []

    def record(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    return opened


@pytest.fixture(autouse=True)
def _no_live_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite never reaches, starts or stops an editor of the operator's.

    Measured 2026-11-24: a command-line test that was given ``--background`` started a REAL detached editor on port
    8190 over the checkout and recorded it in the real ``~/.streamstudio``. Now the lifecycle's own requests are
    refused, the default port always reads free (a test that binds a port of its own is still probed for real), a
    detached start fails the test and so does a look at the processes on the default port; a test about one of these
    replaces the stand-in itself.
    """
    import urllib.error

    from portail import serving

    from streamstudio import cli

    def refuse(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("tests never talk to a live server")

    def no_child(*args: object, **kwargs: object) -> bool:
        raise AssertionError("a test started a detached editor")

    def never_the_operators(port: int) -> List[int]:
        if port == cli.EDITOR_PORT:
            raise AssertionError(f"a test looked up the processes on port {port}, the operator's own")
        return pids(port)

    real, pids = serving.port_in_use, serving.pids_listening
    monkeypatch.setattr(serving, "_urlopen", refuse)
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: port != cli.EDITOR_PORT and real(host, port))
    monkeypatch.setattr(serving, "serve_detached", no_child)
    monkeypatch.setattr(serving, "pids_listening", never_the_operators)
    for word in (serving.CONTINUE_FOLDER, serving.PREPARED):
        monkeypatch.delenv(cli.SPEC.env(word), raising=False)
