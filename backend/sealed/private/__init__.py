"""Private zone: the only code allowed to read the ceiling. Nothing here sends anything.

The ceiling leaves it only through `vault.for_owner`, for the user's dashboard (GET /jobs/{id}/ceiling).
"""
