# Pixel-art agent preset + Aseprite toolbox for DSH

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6)

An agent preset for [DeepSeek Harness (DSH)](https://deepseek.com) that turns it into a **pixel-art
specialist**: it drives **Aseprite** through an MCP server (create canvases, draw exact pixels,
export PNGs) and carries a **pixel-art working method** as a skill (fix the canvas and grid first,
draw a whole image in one Lua pass, keep 1px lines pixel-perfect, edit art as ASCII grids, gate the
output with measurements).

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。The preset/skill display strings shipped in this repo are Chinese.

---

## Why this exists

The hard part of AI pixel art is not generating a picture — it is **precision**: pixel coordinates,
grid anchors, un-blurred 1px lines, palette fidelity, and edits you can reproduce. This project
freezes the discipline that makes that work:

| Capability | How |
|---|---|
| Pixel precision | everything goes through the Aseprite Lua API (`draw_line` with `thickness=1` = Bresenham), never bitmap downscaling |
| Reproducible edits | the source of truth is an **ASCII grid** (`cel-ascii.mjs dump/apply`): change letters, re-render |
| One-pass drawing | a whole image per Lua run instead of many tiny tweaks |
| Structural gate | `check-thin.mjs` rejects 2×2 fully-filled blocks (the pixel-perfect criterion) |
| Measured review | `vision/` ships 24 scripts: diff heatmaps, region stats, palette-mapping error |
| Delivery | export PNG + an ×8 preview and show it to the user |

---

## Install

### A. Download a release (for users)

1. Grab `pixel-preset-kit.zip` from [Releases](../../releases) and unzip it to a stable directory.
2. Run the installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

3. **Restart DSH** (stop the running `dsh web` or desktop app, then start it again).
4. Settings → General → "Agent presets" → the card **像素画（Aseprite）** appears. Pick it for a new task.

### B. Clone the repo (for tinkerers)

```powershell
git clone <this-repo-url> pixel-preset-kit
cd pixel-preset-kit
node scripts/build-kit.mjs                    # writes dist/pixel-preset-kit (runs npm ci for you)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1
```

`scripts/install.ps1` builds the release automatically when it does not find a `manifest.json` next
to itself, so cloning and running it directly also works.

### Options

| Flag | Effect |
|---|---|
| `-DryRun` | print the preset block that would be appended, write nothing |
| `-Profile desktop` | target the desktop-app profile (default: `web`, then `desktop`) |
| `-ToolkitDir "D:\tools\pixel-kit"` | where the toolbox lands (default `%USERPROFILE%\.dsh\pixel-toolkit`) |

The installer verifies the release against `manifest.json`, copies the payload, renders the preset
block, **backs up** the profile patch, appends, then self-checks (markers paired, `preset-pixel`
unique, no tabs, indentation sane) and **rolls back** if any check fails.

---

## Requirements

1. **Windows 10/11** for the installer (macOS/Linux: do it by hand, see below)
2. **DSH installed and started at least once** (it must have created
   `%USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml`)
3. **Node.js ≥ 18** — the MCP server runs on node
4. **Aseprite** — not bundled (commercial software). Auto-detected in the usual install locations;
   otherwise set `ASEPRITE_PATH`
5. Optional: **Python 3 + numpy + Pillow** for `vision/` and `tools/`

---

## Verify (3 checks)

1. The preset card exists and does **not** say "配置失败" (config failed)
2. A new task with this preset lists `mcp__aseprite__*` tools (60 tools in this build)
3. Ask: *draw a 16×16 solid red square, export PNG and show it at 8×* — you should get a PNG and a preview

---

## Layout

```
pixel-preset-kit/
├─ preset/preset-pixel.patch.yml.tpl   # preset declaration template (paths filled at install time)
├─ skills/aseprite-pixel-art/          # the skill: SKILL.md + 4 scripts
├─ vision/                             # 24 image-measurement scripts (numpy + Pillow)
├─ tools/                              # 6 atlas/asset scripts
├─ mcp/                                # the Aseprite MCP server
│  ├─ src/index.ts                     # source (upstream + local patches, 60 tools)
│  ├─ build/index.js                   # prebuilt bundle that gets installed
│  ├─ package.json / package-lock.json # deps (npm ci --omit=dev)
│  └─ LICENSE                          # upstream MIT licence, verbatim
└─ scripts/
   ├─ build-kit.mjs                    # assemble dist/ + manifest (+ optional zip)
   ├─ install.ps1 / uninstall.ps1
   └─ notices.mjs                      # regenerate THIRD-PARTY-NOTICES.md
```

After installation (`%USERPROFILE%\.dsh\pixel-toolkit`):

```
pixel-toolkit/{mcp/{build/index.js,node_modules}, skills/aseprite-pixel-art, vision, tools, tmp}
```

---

## How it is wired (manual install / macOS / Linux)

The installer does exactly two things you can do by hand:

1. **Put the toolbox on disk** — unzip `payload/` (in a release) or copy `mcp/ skills/ vision/ tools/`
   into `~/.dsh/pixel-toolkit/`
2. **Append the preset block** to `~/.dsh/profiles/<profile>/cordis.patch.yml`: take
   `preset/preset-pixel.patch.yml.tpl`, substitute `__PIXEL_TOOLKIT__` with the real toolkit path and
   `__ASEPRITE_PATH_LINE__` with `                ASEPRITE_PATH: <your Aseprite path>` (or an equally
   indented comment), then append the block at the end of the file.

The three rows that matter:

```yaml
      - id: skill-filesystem            # this preset gets its own skill directory
        name: '@deepseek-ai/dsh-skill-filesystem'
        config:
          customSkillDirs:
            - <toolkit>\skills
      - id: mcp-aseprite                # Aseprite MCP over stdio
        name: '@deepseek-ai/dsh-mcp-client'
        config:
          serverName: aseprite
          command: node
          args: [<toolkit>\mcp\build\index.js]
          env:
            ASEPRITE_MCP_TMP: <toolkit>\tmp
            ASEPRITE_PATH: <your Aseprite.exe>
          transport: stdio
```

Two traps worth knowing:

- A preset's plugin list **replaces** the base list; it does not inherit it. Plugins whose config has
  required fields (`tool-fs-search` → `sampleOverCapGlobResults`, `tool-todo` →
  `allowParallelInProgress`) must carry those fields, or the preset shows "config failed".
- Do **not** declare Host-level service providers (`@deepseek-ai/dsh-schedule`, `dsh-time-context`,
  the agent-team plugins) inside a preset: a second `provide()` throws and the preset cannot mount.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Card says "**配置失败**" | open "view config" on the card and read the top diagnostic; usually a missing required config |
| No card at all | DSH was not restarted, or the block went into another profile (`-Profile desktop`) |
| Preset there, no `mcp__aseprite__*` tools | `node -v` ≥ 18? toolkit moved (the args are absolute)? restart DSH |
| `Aseprite executable not found` | install Aseprite or add `ASEPRITE_PATH`, then restart |
| Every run times out | Aseprite's first launch (trial/update dialog). Open it once manually |
| `aseprite-pixel-art` skill not listed | `<toolkit>\skills` missing or wrong depth (must be `skills/<name>/SKILL.md`) |
| Mojibake in installer output | run with `powershell -NoProfile -File ...`; the scripts are UTF-8 with BOM |

Every failure rolls back; a `cordis.patch.yml.bak-pixel-<timestamp>` backup always remains.

---

## Upgrade / uninstall

- **Upgrade**: run `install.ps1` again — it replaces its own marked block in place (and backs up first).
- **Uninstall**:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall.ps1 -RemovePayload
```

---

## Development

```powershell
node scripts/build-kit.mjs          # assemble dist/pixel-preset-kit
node scripts/build-kit.mjs --zip    # + dist/pixel-preset-kit.zip
node scripts/notices.mjs            # regenerate third-party licence list
```

Rebuilding the MCP bundle after editing `mcp/src/index.ts`:

```powershell
cd mcp
npm install                          # includes devDeps (typescript)
npm run build                        # tsc -> build/index.js
```

The build refuses to ship when it finds path leaks (author home/work paths), missing files, a
`.ps1` without BOM, a template missing its placeholders, or an incomplete dependency tree.

---

## Limitations

- **Aseprite itself is not distributed** (commercial), nor any of its assets.
- Examples inside the skill come from the author's own project (relative `projects/...` paths and
  palette case studies); all absolute paths were rewritten to the toolkit location. Mentions of
  `vendor/aseprite-mcp-master/...` refer to the author's source tree — in this repo that is `mcp/`.
- Preset id is `pixel`, order `13`. The installer refuses to run if you already have a hand-written
  preset by that id.
- The toolkit lives at `~/.dsh/pixel-toolkit` and is **shared by all profiles**; use `-ToolkitDir` if
  you want per-profile isolation.
- The installer was only verified on Windows; macOS/Linux users should follow the manual wiring above
  (the MCP server and the skill themselves are cross-platform).
- `mcp/` is a **patched** upstream build: upstream declares 43 tools, this one has 60 (`run_script`,
  indexed-colour/palette ops, `verify_artifact`, …). Patches have not been reported upstream one by one.
- The toolbox is a plain stdio MCP server (`mcp/build/index.js`) plus a Markdown skill, so any MCP
  client could point at it. This repo nevertheless ships and tests **only** the DSH installer and
  preset; adapting other clients is out of scope.

---

## Licence & credits

- This repo (preset, skill, vision/tools scripts, installers): **MIT**, see [LICENSE](LICENSE)
- `mcp/`: **[aseprite-mcp](https://github.com/ayigityol/aseprite-mcp)** by ayigityol, MIT, patched here;
  the upstream licence is kept verbatim in `mcp/LICENSE`
- Bundled npm dependencies: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
- Required but not bundled: **Aseprite**, **DSH**, **Node.js**, **Python + numpy + Pillow**
