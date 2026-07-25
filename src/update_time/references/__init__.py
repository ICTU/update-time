"""Decide which version each pinned reference should update to, honouring its marker, and rewrite it in place."""

from update_time.io.log import attribute_logs_to_caller

# Every module in this package logs on behalf of the updaters, so records point at the updater.
attribute_logs_to_caller(__file__)
