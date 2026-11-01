"""Imported catalogue availability is independent of automatic refresh.

Queries using this predicate join the configured source as ``s``. A removal
record remains authoritative even after cleanup fails; pausing refresh alone
does not revoke access to the already imported catalogue.
"""

AVAILABLE_SQL = 'NOT EXISTS (SELECT 1 FROM m3u_removals r WHERE r.source=s.id)'
