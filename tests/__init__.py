"""Unit tests for Update-time.

If a test reaches the network, the live registry answers it, so the test passes or fails on data nobody wrote
down. Importing this package refuses the network to every test in it. The refusal is a `RuntimeError` rather
than a connection error because `update_time.io.fetch` turns a `requests` exception into a logged `None`, so a
connection error would leave an unmocked request answered with `None` rather than failing the test.

Importing this package also scrubs the credentials of whoever runs the tests from the environment. A mock records
every argument it was called with, and Update-time passes its credentials to `requests` as arguments, so a failing
assertion about a mocked request would print them.
"""

import os
import socket
from typing import NoReturn
from unittest.mock import patch


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
#
# This module runs again whenever the package is dropped from `sys.modules` and imported afresh, and would then
# start a second pair of patches that nothing stops. The refusal already installed is what says whether to install
# one: the socket's own `connect` is a C method descriptor named for the socket, so only a refusal answers to this
# name.
if socket.socket.connect.__name__ != _refuse_connection.__name__:
    patch("socket.socket.connect", _refuse_connection).start()
    patch("socket.getaddrinfo", _refuse_resolution).start()

# Docker Hub's username only travels together with its token, so scrubbing every variable whose name holds TOKEN
# takes that pair with it. The patch is never stopped: no test may put a credential back. Running this again finds
# nothing left to scrub, so the patch it then starts changes nothing, and needs no guard of the kind the refusal
# above has.
patch.dict("os.environ", {name: value for name, value in os.environ.items() if "TOKEN" not in name}, clear=True).start()
