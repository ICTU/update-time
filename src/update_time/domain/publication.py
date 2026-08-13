"""Which sources report a publication date for the releases they resolve.

A source that dates its versions registers its new-version getter with `publication_date_reporting`, naming the
dependencies it dates where it dates only some, and `reports_publication_dates` reads that back for one dependency.
Both the cooldown and the staleness check measure against that date, the cooldown against each candidate's and the
staleness check against the newest version's, so one registration answers for both. A `cooldown` item or a `stale`
directive on a reference whose source dates nothing is then reported as redundant, rather than silently deciding
nothing.
"""

from update_time.primitives.capability import capability

publication_date_reporting, reports_publication_dates = capability("reports_publication_dates")
