"""The user id a read runs as when the caller is not a person.

``tenant_context()`` treats a null ``app.current_user_id`` as an internal worker with no
per-person filtering, so a read for a caller with no person uses
:data:`ANONYMOUS_USER_ID` instead. It is in no record's access lists, so it sees only
public records.
"""

from uuid import UUID

ANONYMOUS_USER_ID = UUID("00000000-0000-0000-0000-000000000000")
