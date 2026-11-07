"""Drive the external package managers (uv, pip-compile, npm, pnpm, Maven) that update dependency manifests.

Each module or subpackage provides one ecosystem's operations — detect which manager governs a project, apply
Update-time's cooldown, run the manager to bump versions, and report what changed — using the `manifests` and
`lock_files` layers to read and write the files and the `sources` layer for changelogs. The updater scripts stay
responsible for discovering the manifest files and drive these operations over them. The managers work too differently
to share one interface, so they share the policy that says which directives steer their dependencies, and little else.
"""
