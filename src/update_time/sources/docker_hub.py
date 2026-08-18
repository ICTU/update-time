"""Docker Hub specifics: credentials and the proprietary push-date API the cooldown relies on.

The OCI protocol exposes no publish date. Its optional `org.opencontainers.image.created` annotation is the image's
*build* time, not when the tag was pushed, and reproducible builds commonly zero it (`SOURCE_DATE_EPOCH`), so it is
not a usable cooldown signal. Docker Hub's proprietary tags API is the only source of a real push date, so the
cooldown can only be enforced for Docker Hub images. This module isolates everything Docker-Hub-specific; the
generic OCI client in `oci` uses it only for that push date and for credentials (which raise the rate limit).
"""

import os
from functools import cache
from typing import TYPE_CHECKING

from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.primitives.timestamp import parse_timestamp

if TYPE_CHECKING:
    from datetime import datetime

_LOG = get_logger("docker hub")

# Docker Hub's proprietary tags API; the only source of a tag's real push date (the OCI protocol exposes none).
_REGISTRY = "https://registry.hub.docker.com"

# The token endpoint the credentials are exchanged at; note that it lives on the user-facing hub.docker.com host,
# not on the _REGISTRY host above.
_AUTH_URL = "https://hub.docker.com/v2/auth/token"


def credentials() -> tuple[str, str] | None:
    """Return the (username, token) Docker Hub credentials if both DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    username = os.environ.get("DOCKER_HUB_USERNAME")
    token = os.environ.get("DOCKER_HUB_TOKEN")
    return (username, token) if username and token else None


@cache
def api_headers() -> dict[str, str]:
    """Return Docker Hub API request headers, with a bearer token when the token endpoint answers with one.

    A run that sets no DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN leaves the requests anonymous. So does an answer
    that carries no token, which Docker Hub's schema allows, since it makes the token an optional field.
    """
    if creds := credentials():
        username, token = creds
        response = fetch(_AUTH_URL, _LOG, method="post", json={"identifier": username, "secret": token})
        if response is not None and (access_token := response.json().get("access_token")):
            return {"Authorization": f"Bearer {access_token}"}
    return {}


def last_pushed(repository: str, tag: str) -> datetime | None:
    """Return the push date of a Docker Hub tag from Docker Hub's proprietary API, used only for the cooldown.

    `repository` is the `namespace/repository` path (e.g. `library/redis`). This proprietary API is the only source
    of a real push date (see the module docstring); when it can't be fetched the response is logged and None is
    returned, which means no cooldown is applied to the tag.
    """
    namespace, repo = repository.split("/", maxsplit=1)
    url = f"{_REGISTRY}/v2/namespaces/{namespace}/repositories/{repo}/tags/{tag}"
    response = fetch(url, _LOG, headers=api_headers())
    if response is None:
        return None
    return parse_timestamp(response.json().get("tag_last_pushed"))
