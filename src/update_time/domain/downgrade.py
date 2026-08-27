"""Which sources can offer a version below the one a reference already pins.

A getter that follows another reference wherever it goes registers with `downgrading`, and `downgrades` reads that
back for one dependency. A bound is judged redundant by sampling the versions above the one a reference pins, so
for a reference that can also move below it that verdict says nothing, and the bound is left unreported.
"""

from update_time.primitives.capability import capability

downgrading, downgrades = capability("downgrades")
