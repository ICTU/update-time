"""Which sources can offer a version below the one a reference already pins."""

from update_time.primitives.capability import capability

downgrading, downgrades = capability("downgrades")
