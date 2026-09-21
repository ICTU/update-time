"""Test fixtures."""

from update_time.markers.marker import _IGNORABLE_SCOPES, _SOURCE_CHECK_SCOPES, Marker
from update_time.sources import github

from tests.mutation import Mutation

# A test needing one value of a kind names the unnumbered constant, and one telling two apart numbers them from 1, so
# the numbering says how many values the test is about. The unnumbered name and the `1` are the same value, so a test
# needing one gets the same value whichever it names, and the name is what tells the reader which case it is. Every
# kind below is named this way, and the numbers line up across the kinds, so `DIGEST2` is built from `HASH2`.
HASH = HASH1 = "a" * 64
HASH2 = "b" * 64
_HASH3 = "c" * 64
DIGEST = DIGEST1 = f"sha256:{HASH1}"
DIGEST2 = f"sha256:{HASH2}"
DIGEST3 = f"sha256:{_HASH3}"

# 40-character hex git commit SHAs, as a GitHub Action `uses:` or a pre-commit hook `rev:` pins to.
COMMIT_SHA = COMMIT_SHA1 = "a" * 40
COMMIT_SHA2 = "b" * 40

# The marker a bare `# update-time: ignore` expresses: every check the marker can hold back is held back.
BARE_IGNORE = Marker(ignored_scopes=_IGNORABLE_SCOPES)

# The directives naming every scope an `ignore` can hold back, so a marker carrying them holds back as much as a
# bare `ignore` does. Derived from the language, so a scope added to it reaches each test that needs the whole set.
EVERY_IGNORABLE_SCOPE = " ".join(f"ignore[{scope}]" for scope in _IGNORABLE_SCOPES)

# The directives naming every scope the reference's own source answers, so a marker carrying them leaves that
# source nothing to be asked for.
EVERY_SOURCE_CHECK_SCOPE = " ".join(f"ignore[{scope}]" for scope in _SOURCE_CHECK_SCOPES)

# A changelog naming version 1.1, so parsing it for 1.1 yields the whole fixture.
CHANGELOG = "## 1.1\n- Fixed ..."

# A regexp matching an `image: name:version` reference, as a Docker Compose or Helm manifest holds one.
IMAGE_REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"

# This mutation drops the one cache that keeps each GitHub API URL to a single request per run. Two test modules
# count those requests, so they share the mutation rather than each spelling it out.
GITHUB_UNCACHED = Mutation(
    github,
    "@cache\ndef _fetch_github(",
    "def _fetch_github(",
    "a GitHub API URL is fetched again for every caller that asks for it",
)
