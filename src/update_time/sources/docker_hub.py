"""Docker Hub specifics: credentials and the proprietary push-date API the cooldown relies on.

The OCI protocol exposes no publish date. Its optional `org.opencontainers.image.created` annotation is the image's
*build* time, not when the tag was pushed, and reproducible builds commonly zero it (`SOURCE_DATE_EPOCH`), so it is
not a usable cooldown signal. Docker Hub's proprietary tags API is the only source of a real push date, so the
cooldown can only be enforced for Docker Hub images. This module isolates everything Docker-Hub-specific; the
generic OCI client in `oci` uses it only for that push date and for credentials (which raise the rate limit).
"""

import os
from datetime import datetime
from functools import cache

import requests

from update_time.io.log import get_logger

LOG = get_logger("docker hub")

# Docker Hub's proprietary tags API; the only source of a tag's real push date (the OCI protocol exposes none).
REGISTRY = "https://registry.hub.docker.com"


def credentials() -> tuple[str, str] | None:
    """Return the (username, token) Docker Hub credentials if both DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    username = os.environ.get("DOCKER_HUB_USERNAME")
    token = os.environ.get("DOCKER_HUB_TOKEN")
    return (username, token) if username and token else None


@cache
def api_headers() -> dict[str, str]:
    """Return Docker Hub API request headers with a bearer token if DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    if creds := credentials():
        username, token = creds
        url = "https://hub.docker.com/v2/auth/token"
        response = requests.post(url, timeout=10, json={"identifier": username, "secret": token})
        response.raise_for_status()
        return {"Authorization": f"Bearer {response.json()['access_token']}"}
    return {}


def last_pushed(repository: str, tag: str) -> datetime | None:
    """Return the push date of a Docker Hub tag from Docker Hub's proprietary API, used only for the cooldown.

    `repository` is the `namespace/repository` path (e.g. `library/redis`). This proprietary API is the only source
    of a real push date (see the module docstring); when it can't be fetched the response is logged and None is
    returned, which means no cooldown is applied to the tag.
    """
    namespace, repo = repository.split("/", maxsplit=1)
    url = f"{REGISTRY}/v2/namespaces/{namespace}/repositories/{repo}/tags/{tag}"
    response = requests.get(url, headers=api_headers(), timeout=10)
    if not response.ok:
        LOG.response(response)
        return None
    pushed = response.json().get("tag_last_pushed")
    return datetime.fromisoformat(pushed) if pushed else None
