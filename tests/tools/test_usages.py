"""Unit tests for the report of where a name is used."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from tools.usages import _ROOTS, Position, _LanguageServerError, _python_files, _usages, main

from tests.helpers import mock_path, patch_pathlib_path


def _framed(*messages: dict) -> bytes:
    """Return the messages as the language server writes them, each behind the headers that frame it."""
    frames = []
    for message in messages:
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        frames.append(
            f"Content-Length: {len(body)}\r\nContent-Type: application/vscode-jsonrpc\r\n\r\n".encode() + body
        )
    return b"".join(frames)


def _unframed(stream: bytes) -> list[dict]:
    """Return the messages a client wrote to the stream, without the headers that frame them."""
    messages = []
    while stream:
        header, _, rest = stream.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1])
        messages.append(json.loads(rest[:length]))
        stream = rest[length:]
    return messages


class PythonFilesTest(unittest.TestCase):
    """Unit tests for the files searched for definitions."""

    def test_every_root_is_searched(self):
        """Test that every root holding Python is searched, for the Python files under it."""
        root = Mock(rglob=Mock(return_value=[Mock()]))
        with patch("tools.usages.Path", Mock(return_value=root)):
            found = _python_files()
        self.assertEqual(len(found), len(_ROOTS))
        self.assertEqual(root.rglob.call_args_list, [call("*.py")] * len(_ROOTS))


class UsagesTest(unittest.TestCase):
    """Unit tests for asking the language server for the usages of a definition."""

    @patch_pathlib_path(read_text="def target():\n    pass\n")
    def test_the_usages_the_server_answers_with(self):
        """Test that the server is asked about the definition's position, and answered when it asks something."""
        user = (Path.cwd() / "src/pkg/user.py").as_uri()
        location = {"uri": user, "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 10}}}
        output = _framed(
            {"id": 1, "result": {"capabilities": {}}},
            {"method": "window/logMessage", "params": {"message": "Starting"}},
            {"id": 7, "method": "workspace/configuration", "params": {"items": []}},
            {"id": 2, "result": [location]},
            {"id": 3, "result": None},
        )
        server_input = MagicMock()
        written = []
        server_input.write.side_effect = written.append
        server = MagicMock(stdin=server_input, stdout=io.BytesIO(output))
        with patch("subprocess.Popen", Mock(return_value=MagicMock(__enter__=Mock(return_value=server)))):
            found = _usages(Position(Path("src/pkg/mod.py"), 0, 4))
        self.assertEqual(found, [Position(Path("src/pkg/user.py"), 2, 4)])
        sent = _unframed(b"".join(written))
        asked = next(message for message in sent if message.get("method") == "textDocument/references")
        self.assertEqual(asked["params"]["position"], {"line": 0, "character": 4})
        self.assertIn({"jsonrpc": "2.0", "id": 7, "result": None}, sent)
        self.assertEqual(
            sent[-2:], [{"jsonrpc": "2.0", "id": 3, "method": "shutdown"}, {"jsonrpc": "2.0", "method": "exit"}]
        )

    @patch_pathlib_path(read_text="def target():\n    pass\n")
    def test_a_server_that_fails(self):
        """Test that a server that does not start, answers with an error, or stops answering raises, saying why."""
        initialized = {"id": 1, "result": {"capabilities": {}}}
        cases = {
            "not started": (FileNotFoundError(2, "No such file or directory", "uv"), "No such file or directory"),
            "an error answer": (_framed(initialized, {"id": 2, "error": {"message": "boom"}}), "boom"),
            "a closed output": (_framed(initialized), "stopped answering"),
        }
        for case, (behaviour, reason) in cases.items():
            with self.subTest(case):
                server = MagicMock(stdout=io.BytesIO(behaviour if isinstance(behaviour, bytes) else b""))
                popen = Mock(return_value=MagicMock(__enter__=Mock(return_value=server)))
                if isinstance(behaviour, OSError):
                    popen.side_effect = behaviour
                with patch("subprocess.Popen", popen), self.assertRaisesRegex(_LanguageServerError, reason):
                    _usages(Position(Path("src/pkg/mod.py"), 0, 4))


class MainTest(unittest.TestCase):
    """Unit tests for the report the command line writes."""

    def report(self, name: str, sources: dict[str, str], usages: list[Position] | Exception) -> int:
        """Report the usages of the name over files holding the sources, the server answering or failing as given."""
        self.written, self.reported = io.StringIO(), io.StringIO()
        files = [mock_path(source, name=path) for path, source in sources.items()]
        self.requested = Mock(side_effect=usages) if isinstance(usages, Exception) else Mock(return_value=usages)
        with (
            patch.object(sys, "argv", ["usages.py", name]),
            patch("tools.usages._python_files", Mock(return_value=files)),
            patch("tools.usages._usages", self.requested),
            redirect_stdout(self.written),
            redirect_stderr(self.reported),
        ):
            return main()

    def test_the_usages_of_a_function(self):
        """Test that a function, named qualified or bare, is looked up at its definition, its usages counted."""
        sources = {
            "src/pkg/mod.py": "def target():\n    pass\n\ntarget()\n",
            "src/pkg/user.py": "from pkg import mod\n\nmod.target()\n",
        }
        usages = [Position(Path("src/pkg/mod.py"), 3, 0), Position(Path("src/pkg/user.py"), 2, 4)]
        for case, name in {"qualified": "pkg.mod.target", "bare": "target"}.items():
            with self.subTest(case):
                self.assertEqual(self.report(name, sources, usages), 0)
                self.requested.assert_called_once_with(Position(Path("src/pkg/mod.py"), 0, 4))
                self.assertEqual(
                    self.written.getvalue(),
                    "src/pkg/mod.py:4: target()\nsrc/pkg/user.py:3: mod.target()\nusages: 2\n",
                )

    def test_a_bare_name_several_definitions_hold(self):
        """Test that a bare name several files define fails the run, naming each, without asking the server."""
        sources = {"src/pkg/mod.py": "def target():\n    pass\n", "src/pkg/other.py": "def target():\n    pass\n"}
        self.assertEqual(self.report("target", sources, []), 1)
        self.assertIn("pkg.mod.target", self.reported.getvalue())
        self.assertIn("pkg.other.target", self.reported.getvalue())
        self.requested.assert_not_called()

    def test_a_server_that_fails(self):
        """Test that a language server that fails stops the run, saying why."""
        failure = _LanguageServerError("the language server stopped answering")
        self.assertEqual(self.report("pkg.mod.target", {"src/pkg/mod.py": "def target():\n    pass\n"}, failure), 1)
        self.assertIn("the language server stopped answering", self.reported.getvalue())

    def test_a_name_nothing_defines(self):
        """Test that a name no file defines fails the run, without asking the language server."""
        self.assertEqual(self.report("pkg.mod.missing", {"src/pkg/mod.py": "def target():\n    pass\n"}, []), 1)
        self.assertIn("nothing defines pkg.mod.missing", self.reported.getvalue())
        self.requested.assert_not_called()

    def test_the_usages_of_a_qualified_method(self):
        """Test that a qualified method is looked up inside its class, its usages on self and an instance reported."""
        sources = {
            "src/pkg/mod.py": (
                "class Holder:\n    def target(self):\n        pass\n\n    def other(self):\n        self.target()\n"
            ),
            "src/pkg/user.py": "from pkg.mod import Holder\n\nHolder().target()\n",
        }
        usages = [Position(Path("src/pkg/mod.py"), 5, 13), Position(Path("src/pkg/user.py"), 2, 9)]
        self.assertEqual(self.report("pkg.mod.Holder.target", sources, usages), 0)
        self.requested.assert_called_once_with(Position(Path("src/pkg/mod.py"), 1, 8))
        self.assertEqual(
            self.written.getvalue(),
            "src/pkg/mod.py:6: self.target()\nsrc/pkg/user.py:3: Holder().target()\nusages: 2\n",
        )
