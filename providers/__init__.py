"""Chat provider adapters and model/provider selection.

Every adapter normalizes its backend's response into a single ``ChatResult``
(``providers.base``), so nothing above this layer knows which backend answered.

Kept deliberately empty of re-exports: ``providers.registry`` imports the
adapters, and re-exporting them here would make that a circular import.
"""
