"""Report where a name is used, as the type checker's language server finds its usages.

Usage: `uv run python -m tools.usages NAME`, which reports every usage this repository makes of the name.
"""

import ast
import json
import subprocess  # nosec
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# The roots holding this repository's Python, which is where one of its own names is used.
_ROOTS = ("src", "tests", "tools")

# A module under this root takes its import path from below it, so `src/update_time/io/log.py` is `update_time.io.log`.
_SOURCE_ROOT = "src"

# The language server that finds the usages, run from the project's environment.
_SERVER = ("uv", "run", "--quiet", "--no-sync", "ty", "server")

# What the report exits with when it could not be made, so the recipe that runs it stops.
_FAILED = 1


@dataclass(frozen=True)
class Position:
    """A place in a file, as the language server counts it: the line and the column, both from zero."""

    path: Path
    line: int
    column: int


def _python_files() -> list[Path]:
    """Return every Python file the repository holds, so a definition outside the package is found too."""
    return [path for root in _ROOTS for path in Path(root).rglob("*.py")]


def _module(path: Path) -> str:
    """Return the import path of the module the file holds, such as `update_time.io.log` for its file under src."""
    parts = Path(str(path)).with_suffix("").parts
    return ".".join(parts[1:] if parts[0] == _SOURCE_ROOT else parts)


def _definitions(files: Iterable[Path]) -> dict[str, Position]:
    """Return the position of each function and class the files define, by its qualified name."""
    return {name: position for file in files for name, position in _defined_in(file).items()}


def _defined_in(file: Path) -> dict[str, Position]:
    """Return the position of each function and class the file defines: its name on its `def` or `class` line."""
    source = file.read_text()
    lines, module = source.splitlines(), _module(file)
    return {
        f"{module}.{name}": Position(
            Path(str(file)), node.lineno - 1, lines[node.lineno - 1].index(node.name, node.col_offset)
        )
        for node, name in _named(ast.parse(source).body, "")
    }


def _named(
    nodes: list[ast.stmt], prefix: str
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, str]]:
    """Yield each function, method, and class among the nodes, its name qualified by the classes around it."""
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node, f"{prefix}{node.name}"
        if isinstance(node, ast.ClassDef):
            yield from _named(node.body, f"{prefix}{node.name}.")


class _LanguageServerError(Exception):
    """A language server that did not start, answered with an error, or stopped answering."""


class _Client:
    """A client of the language server, speaking its protocol over the server's standard input and output."""

    def __init__(self, server_input: IO[bytes], server_output: IO[bytes]) -> None:
        """Talk to the server through its input and output."""
        self._input, self._output, self._last_id = server_input, server_output, 0

    def request(self, method: str, params: dict | None = None) -> object:
        """Send the request and return the result the server answers it with."""
        self._last_id += 1
        self._send({"id": self._last_id, "method": method}, params)
        while (message := self._receive()).get("id") != self._last_id or "method" in message:
            if "method" in message and "id" in message:  # The server asks something of its own: answer it empty
                self._send({"id": message["id"], "result": None})
        if "error" in message:
            reason = message["error"].get("message", "no reason given")
            complaint = f"the language server answered {method} with an error: {reason}"
            raise _LanguageServerError(complaint)
        return message.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send the notification, which the server answers with nothing."""
        self._send({"method": method}, params)

    def _send(self, message: dict, params: dict | None = None) -> None:
        """Write the message with its params, if the method takes any, framed by the header giving its length."""
        body = json.dumps({"jsonrpc": "2.0", **message, **({} if params is None else {"params": params})}).encode()
        self._input.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self._input.flush()

    def _receive(self) -> dict:
        """Read the next message the server writes."""
        length = 0
        while header := self._output.readline().strip():
            if header.lower().startswith(b"content-length:"):
                length = int(header.split(b":")[1])
        if not length:
            complaint = "the language server stopped answering"
            raise _LanguageServerError(complaint)
        return json.loads(self._output.read(length))


def _usages(definition: Position) -> list[Position]:
    """Ask the language server for the usages of the name defined at the position, and return them."""
    root = Path.cwd()
    uri = (root / definition.path).as_uri()
    try:
        with subprocess.Popen(  # noqa: S603 # nosec
            _SERVER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as server:
            # Both streams are pipes, which `Popen` opens whenever it is asked for them:
            client = _Client(cast("IO[bytes]", server.stdin), cast("IO[bytes]", server.stdout))
            client.request("initialize", {"processId": None, "rootUri": root.as_uri(), "capabilities": {}})
            client.notify("initialized", {})
            document = {"uri": uri, "languageId": "python", "version": 1, "text": (root / definition.path).read_text()}
            client.notify("textDocument/didOpen", {"textDocument": document})
            position = {"line": definition.line, "character": definition.column}
            found = cast(
                "list[dict] | None",
                client.request(
                    "textDocument/references",
                    {"textDocument": {"uri": uri}, "position": position, "context": {"includeDeclaration": False}},
                ),
            )
            client.request("shutdown")
            client.notify("exit")
    except OSError as error:
        complaint = f"the language server failed: {error}"
        raise _LanguageServerError(complaint) from error
    return [_position(root, location) for location in found or []]


def _position(root: Path, location: dict) -> Position:
    """Return the position of a location the server answers with, its file relative to the root."""
    path = Path(unquote(urlparse(location["uri"]).path)).relative_to(root)
    start = location["range"]["start"]
    return Position(path, start["line"], start["character"])


def _matches(name: str, definitions: dict[str, Position]) -> list[str]:
    """Return the qualified names the name stands for: itself where it is one, else each qualified name it ends."""
    if name in definitions:
        return [name]
    return [qualified for qualified in definitions if qualified.endswith(f".{name}")]


def _report(message: str) -> None:
    """Write to standard error what stopped the report."""
    sys.stderr.write(f"Error: {message}\n")


def main() -> int:
    """Report where the name given on the command line is used, and how many usages there are."""
    name, files = sys.argv[1], _python_files()
    sources = {str(file): file for file in files}
    lines = []
    definitions = _definitions(files)
    match _matches(name, definitions):
        case []:
            _report(f"nothing defines {name}; check the spelling")
            return _FAILED
        case [qualified]:
            definition = definitions[qualified]
        case several:
            _report(f"{name} names {', '.join(several)}; give one of them in full")
            return _FAILED
    try:
        usages = _usages(definition)
    except _LanguageServerError as error:
        _report(str(error))
        return _FAILED
    for usage in usages:
        text = sources[str(usage.path)].read_text().splitlines()[usage.line].strip()
        lines.append(f"{usage.path}:{usage.line + 1}: {text}")
    sys.stdout.write("\n".join([*lines, f"usages: {len(lines)}"]) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
