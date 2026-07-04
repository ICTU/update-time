"""Drive the external package managers (uv, npm, pnpm) that update dependency manifests.

Each module provides one ecosystem's operations — detect which manager governs a project, apply Update-time's
cooldown, run the manager to bump versions, and report what changed — using the `file_formats` layer to read/write
manifests and the `sources` layer for changelogs. The updater scripts stay responsible for discovering the manifest
files and drive these operations over them. Grouped by ecosystem rather than behind a shared interface, because uv
and the Node managers work too differently to share one (npm and pnpm do share the `PackageManager` dataclass).
"""
