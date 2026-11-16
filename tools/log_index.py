"""Generate the index of the log messages, docs/log-messages.md, from its template and a sample of each message."""

import re
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from tools.markdown import anchors
from update_time.io.log import Logger

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

_ROOT = Path(__file__).parents[1]
LOG_INDEX = _ROOT / "docs" / "log-messages.md"
_TEMPLATE = _ROOT / "docs" / "log-messages.md.in"
_TEMPLATE_NAME = _TEMPLATE.relative_to(_ROOT)

# A placeholder in the template names the message whose heading and sample replace it.
_PLACEHOLDER = re.compile(r"@@(?P<name>[a-z]+(?:-[a-z]+)*)@@")

# A link from the index to a section of the README, captured by the anchor it points at.
_README_LINK = re.compile(r"\]\(\.\./README\.md(#[^)]+)\)")


def generate_log_index(samples: Mapping[str, str], readme: str) -> str:
    """Return the index of the log messages, from its template, the sample of each name, and the README it links to."""
    template = _TEMPLATE.read_text()
    _stop_on_dead_links(template, readme)
    names = [message.name for message in Logger.messages()]
    return _log_index(template, samples, names)


def _stop_on_dead_links(template: str, readme: str) -> None:
    """Stop the generator where the template links to an anchor that the headings of the README do not provide."""
    dead = set(_README_LINK.findall(template)) - anchors(readme)
    _stop_on(f"{_TEMPLATE_NAME} links to README sections that do not exist", dead)


def _log_index(template: str, samples: Mapping[str, str], names: Collection[str]) -> str:
    """Return the template with each placeholder replaced by the heading and the sample of the message it names."""
    placed = Counter(placeholder["name"] for placeholder in _PLACEHOLDER.finditer(template))
    _stop_unless_complete(placed, samples, set(names))
    return _PLACEHOLDER.sub(lambda placeholder: _entry(placeholder["name"], samples[placeholder["name"]]), template)


def _stop_unless_complete(placed: Counter[str], samples: Mapping[str, str], names: set[str]) -> None:
    """Stop the generator, unless each name has one placeholder and a sample, and each placeholder names a message."""
    problems = {
        f"{_TEMPLATE_NAME} has no placeholder for these log messages": names - placed.keys(),
        f"{_TEMPLATE_NAME} has more than one placeholder for these log messages": {
            name for name, count in placed.items() if count > 1
        },
        f"{_TEMPLATE_NAME} has placeholders that do not name a log message": placed.keys() - names,
        "tools/log_samples.py does not log a sample of these log messages": names - samples.keys(),
    }
    for problem, culprits in problems.items():
        _stop_on(problem, culprits)


def _stop_on(problem: str, culprits: set[str]) -> None:
    """Stop the generator where there are culprits, naming the problem and each of them."""
    if culprits:
        sys.exit(f"{problem}: {', '.join(sorted(culprits))}")


def _entry(name: str, sample: str) -> str:
    """Return the heading naming the message, and its sample in a console block."""
    return f"### {name}\n\n```console\n{sample}\n```"
