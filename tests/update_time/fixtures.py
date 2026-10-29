"""Test fixtures."""

from datetime import UTC, datetime, timedelta

from update_time.markers.marker import parse_directives
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
COMMIT_SHA3 = "c" * 40

# The marker setting the risk level `none`, which silences every vulnerability warning.
VULNERABILITY_LEVEL_NONE = parse_directives("set[vulnerability-level=none]")

# The marker setting a staleness threshold of 90 days.
STALE_AFTER_90 = parse_directives("set[stale-after=90]")

# The marker setting a cooldown of 30 days.
COOLDOWN_30 = parse_directives("set[cooldown=30]")

# A publication date old enough that the default staleness threshold warns about it, and one inside the default
# cooldown.
STALE_DATE = datetime.now(UTC) - timedelta(days=512)
FRESH_DATE = datetime.now(UTC)

# A publication date past the default cooldown that the default staleness threshold passes over, and a threshold of
# 90 days does not.
PAST_COOLDOWN_DATE = datetime.now(UTC) - timedelta(days=100)

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
