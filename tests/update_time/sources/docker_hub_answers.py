"""What Docker Hub answers about a tag, and the version a run renders for it."""

from typing import TYPE_CHECKING

from tests.update_time.fixtures import PAST_COOLDOWN_DATE
from tests.update_time.io.log_fields import published_version

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.formats.json import JSON


def docker_tag(
    name: str, digest: str = "", *, tag_last_pushed: datetime | None = PAST_COOLDOWN_DATE, **extra: JSON
) -> dict[str, JSON]:
    """Return a single-tag Docker Hub API result for the tag, with an optional digest, a push date, and extra fields."""
    pushed = {"tag_last_pushed": tag_last_pushed.isoformat()} if tag_last_pushed else {}
    return {"name": name, **({"digest": digest} if digest else {}), **pushed, **extra}


def docker_hub_version(version: str) -> str:
    """Return the version as a new-version line renders it for a tag `docker_tag` dates by default."""
    return published_version(version, PAST_COOLDOWN_DATE)
