"""OAuth device-code logins and the shared token store.

Tokens for every provider live in one gitignored file (``brain/auth.json``)
via ``auth.store``; ``auth.codex`` and ``auth.github`` run the device flows.
"""
