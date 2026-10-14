"""The port probe ``streamstudio`` runs before it serves: a taken port is refused before any work.

The probe BINDS (with ``SO_REUSEADDR``) instead of connecting, so it tells "nothing is listening" apart from "listening
but not accepting yet". Both directions are pinned against a REAL socket. What the refusal prints — a clickable URL and
a command the CLI accepts — is pinned where the editor is served (``tests/test_cli_editor.py::TestServingTheCanvas``).
"""

import socket
from typing import Iterator

import pytest

from streamstudio import cli


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A really-listening socket on a free port — the probe is exercised, never mocked."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((cli.STUDIO_HOST, 0))
        held.listen(1)
        yield int(held.getsockname()[1])


@pytest.fixture
def free_port() -> int:
    """A port nothing listens on: bound, read, and released before the test runs."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.STUDIO_HOST, 0))
        port = int(probe.getsockname()[1])
    return port


def test_a_listening_socket_reads_as_in_use(busy_port: int) -> None:
    assert cli._port_in_use(busy_port) is True


def test_a_port_nothing_listens_on_reads_as_free(free_port: int) -> None:
    assert cli._port_in_use(free_port) is False
