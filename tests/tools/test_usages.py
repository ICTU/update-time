"""Unit tests for the report of where a name is used."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from tools import usages as usages_module
from tools.usages import _ROOTS, Position, _LanguageServerError, _python_files, _usages, main

from mutation_time.mutation import kills

from tests.helpers import mock_path, patch_pathlib_path

# The module that defines the name the tests look up, and a module that uses it.
_DEFINING_MODULE = Path("src/pkg/mod.py")
_USING_MODULE = Path("src/pkg/user.py")


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
            found = _usages(Position(_DEFINING_MODULE, 0, 4))
        self.assertEqual(found, [Position(_USING_MODULE, 2, 4)])
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
                    _usages(Position(_DEFINING_MODULE, 0, 4))


class MainTest(unittest.TestCase):
    """Unit tests for the report the command line writes."""

    def report(self, name: str, sources: dict[Path, str], usages: list[Position] | Exception) -> int:
        """Report the usages of the name over files holding the sources, the server answering or failing as given."""
        self.written, self.reported = io.StringIO(), io.StringIO()
        files = [mock_path(source, name=str(path)) for path, source in sources.items()]
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
            _DEFINING_MODULE: "def target():\n    pass\n\ntarget()\n",
            _USING_MODULE: "from pkg import mod\n\nmod.target()\n",
        }
        usages = [Position(_DEFINING_MODULE, 3, 0), Position(_USING_MODULE, 2, 4)]
        for case, name in {"qualified": "pkg.mod.target", "bare": "target"}.items():
            with self.subTest(case):
                self.assertEqual(self.report(name, sources, usages), 0)
                self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 0, 4))
                self.assertEqual(
                    self.written.getvalue(),
                    "src/pkg/mod.py:4: target()\nsrc/pkg/user.py:3: mod.target()\nusages: 2\n",
                )

    @kills.replace(
        usages_module._targets, "return [target]", "return []", "the report says nothing defines an annotated constant"
    )
    def test_the_usages_of_an_annotated_constant(self):
        """Test that an annotated module-level constant is looked up at its name."""
        self.assertEqual(self.report("pkg.mod.TARGET", {_DEFINING_MODULE: "TARGET: int = 1\n"}, []), 0)
        self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 0, 0))

    @kills.replace(
        usages_module._named,
        'yield f"{prefix}{name.id}"',
        "yield target.id",
        "the report says nothing defines a class attribute named through its class",
    )
    def test_the_usages_of_a_class_attribute(self):
        """Test that a class attribute is looked up inside its class."""
        source = "class Holder:\n    TARGET = 1\n"
        self.assertEqual(self.report("pkg.mod.Holder.TARGET", {_DEFINING_MODULE: source}, []), 0)
        self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 1, 4))

    @kills.delete_statement(
        usages_module._named,
        "yield from _instance_attributes(node, prefix, path)",
        "the report says nothing defines an attribute that `__init__` assigns",
    )
    @kills.delete(
        usages_module._instance_attributes,
        ' and target.value.id == "self"',
        "an attribute `__init__` assigns on another object is taken for one of the instance",
    )
    @kills.delete(
        usages_module._instance_attributes,
        " - len(target.attr)",
        "the language server is asked about the end of the attribute's name rather than its start",
    )
    def test_the_usages_of_an_instance_attribute(self):
        """Test that an attribute `__init__` assigns on the instance, not on another object, is looked up there."""
        source = (
            "class Holder:\n    def __init__(self, other):\n        self.target = 1\n"
            "        local = 2\n        other.target = local\n"
        )
        self.assertEqual(self.report("pkg.mod.Holder.target", {_DEFINING_MODULE: source}, []), 0)
        self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 2, 13))

    def test_a_local_assignment_defines_nothing(self):
        """Test that a name a function body assigns is not a definition, so a constant of that name is unambiguous."""
        source = "TARGET = 1\n\n\ndef holder():\n    TARGET = 2\n"
        self.assertEqual(self.report("TARGET", {_DEFINING_MODULE: source}, []), 0)
        self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 0, 0))

    @kills.replace(usages_module._targets, "return [name]", "return []", "the report says nothing defines a type alias")
    def test_the_usages_of_a_type_alias(self):
        """Test that a type alias is looked up at its name."""
        self.assertEqual(self.report("pkg.mod.Target", {_DEFINING_MODULE: "type Target = int\n"}, []), 0)
        self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 0, 5))

    @kills.delete(
        usages_module._assigned_names,
        "target.elts if isinstance(target, ast.Tuple) else ",
        "the report says nothing defines a name that an assignment to a tuple binds",
        case="FIRST",
    )
    @kills.replace(
        usages_module._named,
        "Position(path, name.lineno - 1, name.col_offset)",
        "Position(path, name.lineno - 1, target.col_offset)",
        "each name of a tuple is looked up at the position of the first, so the report counts that name's usages",
        case="SECOND",
    )
    def test_each_name_of_a_tuple_target_is_looked_up_where_it_is_written(self):
        """Test that each name an assignment to a tuple defines is looked up at its own position."""
        for name, column in (("FIRST", 0), ("SECOND", 7)):
            with self.subTest(name):
                self.assertEqual(self.report(f"pkg.mod.{name}", {_DEFINING_MODULE: "FIRST, SECOND = 1, 2\n"}, []), 0)
                self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 0, column))

    @kills.force_true(
        usages_module._assigned_names,
        "isinstance(element, ast.Name)",
        "a module-level assignment to an attribute ends the report with a traceback",
        raises="AttributeError: 'Attribute' object has no attribute 'id'. Did you mean '.value.id' instead of '.id'?",
    )
    def test_an_assignment_to_an_attribute_defines_nothing(self):
        """Test that an assignment to an attribute is not looked up as a definition of the attribute."""
        self.assertEqual(self.report("TARGET", {_DEFINING_MODULE: "holder.TARGET = 1\n"}, []), 1)
        self.assertIn("nothing defines TARGET", self.reported.getvalue())
        self.requested.assert_not_called()

    @kills.delete_statement(
        usages_module._module,
        'parts = parts[:-1] if parts[-1] == "__init__" else parts',
        "the report says nothing defines a name that the `__init__.py` of a package defines",
    )
    def test_a_function_the_init_of_a_package_defines_is_looked_up_by_the_package_s_name(self):
        """Test that a function the `__init__.py` of a package defines is looked up there, named by the package."""
        package = Path("src/pkg/__init__.py")
        self.assertEqual(self.report("pkg.target", {package: "def target():\n    pass\n"}, []), 0)
        self.requested.assert_called_once_with(Position(package, 0, 4))

    def test_a_bare_name_several_definitions_hold(self):
        """Test that a bare name several files define fails the run, naming each, without asking the server."""
        sources = {_DEFINING_MODULE: "def target():\n    pass\n", Path("src/pkg/other.py"): "def target():\n    pass\n"}
        self.assertEqual(self.report("target", sources, []), 1)
        self.assertIn("pkg.mod.target", self.reported.getvalue())
        self.assertIn("pkg.other.target", self.reported.getvalue())
        self.requested.assert_not_called()

    def test_a_server_that_fails(self):
        """Test that a language server that fails stops the run, saying why."""
        failure = _LanguageServerError("the language server stopped answering")
        self.assertEqual(self.report("pkg.mod.target", {_DEFINING_MODULE: "def target():\n    pass\n"}, failure), 1)
        self.assertIn("the language server stopped answering", self.reported.getvalue())

    def test_a_name_nothing_defines(self):
        """Test that a name no file defines fails the run, without asking the language server."""
        self.assertEqual(self.report("pkg.mod.missing", {_DEFINING_MODULE: "def target():\n    pass\n"}, []), 1)
        self.assertIn("nothing defines pkg.mod.missing", self.reported.getvalue())
        self.requested.assert_not_called()

    def test_the_usages_of_a_qualified_method(self):
        """Test that a qualified method is looked up inside its class, its usages on self and an instance reported."""
        sources = {
            _DEFINING_MODULE: (
                "class Holder:\n    def target(self):\n        pass\n\n    def other(self):\n        self.target()\n"
            ),
            _USING_MODULE: "from pkg.mod import Holder\n\nHolder().target()\n",
        }
        usages = [Position(_DEFINING_MODULE, 5, 13), Position(_USING_MODULE, 2, 9)]
        self.assertEqual(self.report("pkg.mod.Holder.target", sources, usages), 0)
        self.requested.assert_called_once_with(Position(_DEFINING_MODULE, 1, 8))
        self.assertEqual(
            self.written.getvalue(),
            "src/pkg/mod.py:6: self.target()\nsrc/pkg/user.py:3: Holder().target()\nusages: 2\n",
        )
