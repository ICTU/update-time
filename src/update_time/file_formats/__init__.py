"""Read, write, and parse the specific manifest file formats Update-time updates.

Boundary — mechanics, not semantics: a `file_formats` module owns operations that need a format library or the
serialized form itself, i.e. parsing a manifest and format-preserving reads/writes of specific constructs (e.g.
tomlkit edits that keep comments, or a regex rewrite over the raw text). Pure queries over the already-parsed
structure stay with the caller (an updater or, later, a package manager): they navigate the dict a `read` returns
and need no format library. So `read`/`set_tool_key`/`rewrite_pinned_versions` live here, while predicates like
`has_node_engine` or `python_manager`'s `[tool.poetry]` check — plain dict navigation — do not.

The package has two tiers, kept together for now. Generic serialization formats parse a format: `yaml.read` parses
any YAML, and the tomllib/json parsing sits behind `pyproject_toml.read` and `package_json.read`. Package-manifest
specifics handle one manifest: `pyproject_toml`'s `tool_key`, `set_tool_key`, and `rewrite_pinned_versions`, and
`requirements_txt.is_compiled`.
They are kept together because each parsed format currently serves a single manifest and the generic halves are
one-liners, so separate `formats/` and `manifests/` sub-packages would be structure ahead of need. Split them when
a second manifest has to parse the same format, or when the manifest-specific logic outgrows the format mechanics.
"""
