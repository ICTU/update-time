"""Unit tests for Update-time.

If a test reaches the network, the live registry answers it, so the test passes or fails on data nobody wrote
down. Importing this package refuses the network to every test in it. The refusal is a `RuntimeError` rather
than a connection error because `update_time.io.fetch` turns a `requests` exception into a logged `None`, so a
connection error would leave an unmocked request answered with `None` rather than failing the test.
"""

from typing import TYPE_CHECKING, NoReturn
from unittest.mock import patch

if TYPE_CHECKING:
    import socket


def _refuse(address: object) -> NoReturn:
    """Raise the refusal, naming the address the test tried to reach."""
    message = f"test tried to reach the network at {address}"
    raise RuntimeError(message)


def _refuse_connection(_self: socket.socket, address: object) -> NoReturn:
    _refuse(address)


def _refuse_resolution(host: object, *_args: object, **_kwargs: object) -> NoReturn:
    """Refuse to resolve the host name, whatever port, family, and flags the lookup passes with it."""
    _refuse(host)


# A request to a host name is refused at the lookup, before it reaches the resolver; one to a literal address needs
# no lookup and is refused at the socket. Neither patch is ever stopped: no test may lift the refusal.
patch("socket.socket.connect", _refuse_connection).start()
patch("socket.getaddrinfo", _refuse_resolution).start()
