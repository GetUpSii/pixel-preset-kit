# Changelog

All notable changes to this project are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [1.0.0] - 2026-10-27

First public release.

### Added

- **Agent preset `pixel` ("像素画（Aseprite）")** for DSH: persona with nine hard rules, file/shell/job
  tools, its own skill directory, the Aseprite MCP client, plan mode and compaction groups.
- **Skill `aseprite-pixel-art`**: canvas/grid discipline, one-pass Lua drawing, pixel-perfect thin
  lines, ASCII-grid round-trip editing, output gates, and a field-notes section of failure modes.
  Scripts: `aseprite-lua.mjs` (Lua runner with a file-based result channel and failure retention),
  `cel-ascii.mjs` (dump/apply ASCII grids, with `--selftest`), `preview.mjs` (×N preview),
  `check-thin.mjs` (2×2 block detector).
- **`vision/`**: 24 measurement scripts (diff heatmaps, region statistics, palette mapping error,
  light audits, style profiles) and **`tools/`**: 6 atlas/asset scripts.
- **MCP server bundle**: `aseprite-mcp`, vendored and patched — 60 tools (upstream: 43), including
  `run_script`, indexed-colour/palette operations and `verify_artifact`. Prebuilt `build/index.js`
  plus `src/` and `npm ci`-reproducible dependencies.
- **Installers**: `scripts/install.ps1` (verify manifest → copy toolbox → render preset block →
  back up → append → self-check, roll back on failure) and `scripts/uninstall.ps1` (exact block
  removal, byte-identical restore). Idempotent: re-running upgrades in place.
- **Build pipeline**: `scripts/build-kit.mjs` assembles `dist/pixel-preset-kit` with a SHA-256
  manifest and refuses to ship on path leaks, missing files, a BOM-less `.ps1` or missing deps;
  `--zip` also produces the release archive.
- **Docs**: English and Chinese READMEs, third-party notices generator.

### Notes

- Aseprite itself is **not** bundled (commercial software).
- Windows-only installer; the MCP server, skill and scripts are cross-platform.
