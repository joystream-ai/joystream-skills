# Plugins

A plugin is one use case that composes more than one skill. Each plugin lives in
`plugins/<name>/` with `.claude-plugin/plugin.json` (whose `name` must equal the
directory name), optional `.mcp.json`, and its skills under `skills/<skill>/SKILL.md`.

Register every plugin in `.claude-plugin/marketplace.json`; the importer follows that
manifest, not the directory tree.
