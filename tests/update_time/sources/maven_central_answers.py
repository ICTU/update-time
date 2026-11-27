"""What Maven Central answers: an artefact's directory listing and the poms beside its versions."""

from typing import TYPE_CHECKING, Final
from unittest.mock import Mock
from urllib.parse import urlparse

from tests.http_mocks import json_response, mock_response, patching_get
from tests.update_time.manifests.manifest_contents import pom_parent

from .github_answers import github_endpoint

if TYPE_CHECKING:
    from datetime import datetime


def maven_central_version_row(version: str, published: datetime) -> str:
    """Return the row for a version, which the repository serves as a directory, sized with a dash."""
    return maven_central_row(f"{version}/", published, "-")


def maven_central_row(name: str, published: datetime, size: str) -> str:
    """Return a row of the listing, padded out to the date column.

    The repository dates the row in GMT without naming the zone, and closes it with the entry's size.
    """
    return maven_central_dated_row(name, f"{published:%Y-%m-%d %H:%M}", size)


def maven_central_dated_row(name: str, published: str, size: str) -> str:
    """Return a row of an artefact's directory listing, dated exactly as given rather than by an instant."""
    link = f'<a href="{name}" title="{name}">{name}</a>'
    padding = " " * max(1, 50 - len(name))
    return f"{link}{padding}{published}   {size}\n"


def maven_central_pom_url(artefact: str, version: str) -> str:
    """Return the URL Maven Central serves the pom beside the artefact's version at."""
    artifact_id = artefact.split(":")[1]
    return f"{maven_central_listing_url(artefact)}{version}/{artifact_id}-{version}.pom"


def maven_central_listing_url(artefact: str) -> str:
    """Return the URL Maven Central serves the artefact's directory listing at."""
    group_id, artifact_id = artefact.split(":")
    # The base URL Maven Central serves its artefacts under.
    return f"https://repo1.maven.org/maven2/{group_id.replace('.', '/')}/{artifact_id}/"


def maven_central_listing(*rows: str) -> str:
    """Return the directory listing the repository serves for an artefact, holding the given rows.

    The listing opens with a link to the parent directory.
    """
    return f'<html><body><pre>\n<a href="../">../</a>\n{"".join(rows)}</pre></body></html>'


def maven_central_pom(
    scm: str = "", tag: str = "url", project_url: str = "", parent: str = "", properties: str = ""
) -> str:
    """Return the pom beside a version, naming the source repository in the given child of its `<scm>` element.

    An scm given as nothing is a pom that does not declare an `<scm>` element at all, as guava's own pom does not.
    A project URL is the project's own `<url>` element, where guava's pom names its repository instead. A parent,
    given as `groupId:artifactId:version`, is the pom's `<parent>` element.
    """
    url = f"  <url>{project_url}</url>\n" if project_url else ""
    declared = f"  <scm>\n    <{tag}>{scm}</{tag}>\n  </scm>\n" if scm else ""
    elements = f"{pom_parent(parent)}{properties}{url}{declared}"
    return f'<project xmlns="http://maven.apache.org/POM/4.0.0">\n{elements}</project>\n'


# The source repositories poms name: guava's own on GitHub, and one outside GitHub, where commons-lang3's lives.
GUAVA_SCM: Final[str] = "scm:git:https://github.com/google/guava.git"


GITBOX_SCM: Final[str] = "scm:git:https://gitbox.apache.org/repos/asf/commons-lang.git"


@patching_get
def patch_maven_central(listing: str, pom: str | None, *, archived: bool = False, releases: list | None = None) -> Mock:
    """Patch Session.get to serve an artefact's listing, a version's pom, and the GitHub repository it names.

    GitHub serves the given releases for every repository, and answers the releases request non-OK where none are given.

    Each request is answered by the URL it names, so a test needs no expectation about the order they are made in.
    A pom given as None is one the repository does not serve.
    """
    github = {"releases": releases, "": {"archived": archived}}

    def serve(url: str, **_kwargs: object) -> Mock:
        if urlparse(url).hostname == "api.github.com":
            return json_response(github.get(github_endpoint(url)), url)
        if not url.endswith(".pom"):
            return mock_response(text=listing, url=url)
        if pom is None:
            return mock_response(ok=False, status_code=404, reason="Not Found", url=url, text="")
        return mock_response(text=pom, content=pom.encode(), url=url)

    return Mock(side_effect=serve)
