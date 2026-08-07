"""Which sources can observe that a version was yanked.

A source whose versions can carry a yank state registers its new-version getter with `yank_reporting`, and
`reports_yanks` reads the fact back, so an `ignore[yanked]` marker on a reference resolved through any other source
can be reported as redundant instead of silently holding nothing back. Only the getters that resolve a marked
reference take part.
"""

from update_time.domain.capability import capability

yank_reporting, reports_yanks = capability("reports_yanks")
