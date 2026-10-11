"""Per-reference cooldowns, as markers set them."""

from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN

if TYPE_CHECKING:
    from update_time.markers.marker import Marker


def cooldown_days(marker: Marker) -> int:
    """Return the number of days the reference holds back what was published: its own cooldown, or the run's."""
    return marker.cooldown.value_or(COOLDOWN.get())
