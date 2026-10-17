"""The network settings a run inherits from its environment."""

import os
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import getproxies

if TYPE_CHECKING:
    from update_time.io.log import Logger

# What a proxy's user and password are logged as, so the log shows that a proxy has them but not what they are.
_REDACTED = "<redacted>"
# What a proxy URL that does not split is logged as, since a password in it cannot be found to redact.
_UNREADABLE = "<unreadable>"


def report_network_settings(logger: Logger) -> None:
    """Log the proxies, the hosts they skip, and the CA bundle, as requests reads them from the environment."""
    if not logger.logs_debug():
        return
    proxies = getproxies()
    no_proxy = proxies.pop("no", "")
    # requests reads REQUESTS_CA_BUNDLE first and CURL_CA_BUNDLE second, and falls back on certifi's bundle.
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or ""
    logger.network_settings({scheme: _redacted_url(url) for scheme, url in proxies.items()}, no_proxy, ca_bundle)


def _redacted_url(url: str) -> str:
    """Return the URL with its user and password redacted, read as an HTTP proxy where it does not name a scheme."""
    try:
        parts = urlsplit(url if "://" in url else f"http://{url}")
    except ValueError:
        return _UNREADABLE
    address = parts.netloc.rpartition("@")[2]
    credentials = ":".join(_REDACTED for part in (parts.username, parts.password) if part is not None)
    user_info = f"{credentials}@" if credentials else ""
    return f"{parts.scheme}://{user_info}{address}"
