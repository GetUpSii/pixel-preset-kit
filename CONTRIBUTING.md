# Contributing

Thanks for taking a look. This is a small project with a narrow scope: making a DSH agent draw
**precise** pixel art through Aseprite.

## What is in scope

- Pixel-art technique that can be *checked* (grid round-trips, 2×2 block detection, palette error,
  measured diffs) — new gates and scripts are welcome.
- Aseprite MCP tools that remove a round-trip through Python or PNG re-import.
- Installer/build hardening: more failure modes that roll back cleanly, fewer assumptions about the
  user's machine.
- Translations of the README (the preset/skill display strings are Chinese for now).

## What is out of scope

- Bundling Aseprite or its assets.
- "Make it prettier" prompts that cannot be verified — the skill deliberately refuses aesthetic
  judgement and leaves art direction to the user.
- Host-level DSH service providers inside the preset (see the README traps section).

## Reporting a bug

Please include:

1. DSH version and `node -v`, plus the output of `node -e "console.log(process.platform)"`
2. What you ran (`install.ps1` flags) and the **full** installer output
3. If the preset card shows "配置失败", the diagnostic line from the card's "view config" panel
4. If a drawing step failed: the `_fail/*.note.txt` file the MCP server retained (it holds the raw
   Aseprite error and the exact Lua that ran)

## Working on the code

```powershell
node scripts/build-kit.mjs          # assemble dist/pixel-preset-kit (runs npm ci in mcp/)
node scripts/build-kit.mjs --zip    # + dist/pixel-preset-kit.zip
node scripts/notices.mjs            # regenerate THIRD-PARTY-NOTICES.md
npm test                            # installer round-trip against a fake %USERPROFILE%
cd mcp; npm install --ignore-scripts; node node_modules/typescript/bin/tsc -p tsconfig.json
                                    # rebuild the MCP bundle after editing mcp/src/index.ts
```

Before opening a PR:

- `node scripts/build-kit.mjs` must pass its own gates (no path leaks, no missing files).
- If you touched `scripts/install.ps1` or `uninstall.ps1`, run them against a **copy** of a profile
  patch — never against a live one. The test flow used here: build a fake `%USERPROFILE%`, install,
  install again (must stay a single block), uninstall (patch must return byte-identical).
- Keep `.ps1` files UTF-8 **with BOM** and Windows PowerShell 5.1-compatible (no ternary, no `??`).
- Commit messages: imperative, one concern per commit; mention the preset id or script you touched.

## Licence

By contributing you agree your changes are licensed under the MIT licence in [LICENSE](LICENSE).
