# 像素画（Aseprite）Agent 预设 + 工具箱

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6)

给 [DSH（DeepSeek Harness）](https://deepseek.com) 加一个**像素画专职 Agent**：它带着
**Aseprite MCP 工具**（直接在 Aseprite 里建稿、画像素、导出）+ 一套**像素画工作法技能**
（先定画布与网格、一段 Lua 批量成图、细线像素完美、ASCII 网格改图、出图闸门与看图量化）。

装上以后，新建任务时选「像素画（Aseprite）」，就能对它说「画一个 32×32 的蘑菇，配色用这几个」，
直接拿到 PNG 与 ×8 放大预览。

> English: [README.md](README.md)。本仓库的界面文案（预设名/技能名）是中文。

---

## 一、它解决什么

让 AI 画像素画，难点不在"生成图"，而在**精确**：像素坐标、网格锚点、1px 线不能糊、
调色板不能跑色、改图要能复原。这个项目把"能用"到"准确"之间那堆手工纪律固化下来：

| 能力 | 做法 |
|---|---|
| 像素精度 | 所有绘制走 Aseprite Lua API（`draw_line` thickness=1 = Bresenham），不用位图缩放糊弄 |
| 可复现改图 | 造型真源是 **ASCII 网格**（`cel-ascii.mjs` dump/apply），改图 = 改字母再出图 |
| 批量成图 | 一张图一段 Lua 一次画完，避免小步多轮返工 |
| 结构自检 | `check-thin.mjs` 检测 2×2 全填块（像素完美判据），出图后自动跑 |
| 差异量化 | `vision/` 24 个脚本：图像差分热图、区域统计、调色板映射误差 |
| 交付 | 导出 PNG + ×8 放大预览，直接贴给用户 |

---

## 二、安装

### 方式 A：下载 release（推荐给使用者）

1. 到 [Releases](../../releases) 下载 `pixel-preset-kit.zip` 并解压到固定目录
2. 跑安装器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

3. **重启 DSH**（关掉正在跑的 `dsh web` / 桌面版，再启动）
4. 设置 → 通用 → 「Agent 预设」→ 选中卡片「像素画（Aseprite）」，新建任务时选它

### 方式 B：克隆仓库（推荐给改东西的人）

```powershell
git clone <本仓库地址> pixel-preset-kit
cd pixel-preset-kit
node scripts/build-kit.mjs                     # 产出 dist/pixel-preset-kit（会自动 npm ci）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1
```

`scripts/install.ps1` 发现旁边没有 `manifest.json` 时会**自动先构建**，所以 `git clone` 后直接跑它也行。

### 常用参数

| 参数 | 作用 |
|---|---|
| `-DryRun` | 只打印将要追加的 preset 块，**不写任何文件** |
| `-Profile desktop` | 装到桌面版 profile（默认自动挑 `web`，其次 `desktop`） |
| `-ToolkitDir "D:\tools\pixel-kit"` | 工具包落地目录（默认 `%USERPROFILE%\.dsh\pixel-toolkit`） |

安装器会：验包 → 查 node / Aseprite → 复制 payload → 生成 preset 块 →
**备份补丁** → 追加 → 结构自校验（标记成对、`preset-pixel` 唯一、无 tab、缩进正确）；
**任何一条不过就自动回滚**。

---

## 三、前置条件

1. **Windows 10/11**（安装脚本是 PowerShell；macOS / Linux 见第六节手工做法）
2. **DSH 已安装，且至少启动过一次**（要它生成 `%USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml`）
3. **Node.js ≥ 18**（`node -v`；推荐 20/22）—— MCP 服务端由 node 启动
4. **Aseprite**（自备，本仓库**不含**，它是付费软件）
   - 装在默认位置（`C:\Program Files\Aseprite`、Steam 库、`%LOCALAPPDATA%\Programs\Aseprite`）→ 自动探测
   - 装在别处 → 安装前 `setx ASEPRITE_PATH "D:\你的\Aseprite.exe"`，或装完手改补丁里那一行
5. 可选：**Python 3 + numpy + Pillow**（`vision/`、`tools/` 脚本用；DSH 自带解释器可直接用）

---

## 四、验收（别跳过，3 条判据）

1. **卡片在**：设置 → 通用 → Agent 预设里有「像素画（Aseprite）」，且**不是**「配置失败」
2. **工具在**：新建任务选这个预设，工具列表里能看到 `mcp__aseprite__*`（本包实测 **60 个**工具）
3. **能出图**：对它说「画一个 16×16 的纯红方块，导出 PNG 并放大 8 倍给我看」，
   应看到它调 Aseprite 建稿、导出、贴出 ×8 预览

三条都过才算装好。第 1 条不过 → 见第七节第一行。

---

## 五、目录结构

仓库：

```
pixel-preset-kit/
├─ preset/preset-pixel.patch.yml.tpl   # preset 声明模板（安装时填路径）
├─ skills/aseprite-pixel-art/          # 主技能：SKILL.md + 4 个脚本
├─ vision/                             # 24 个看图量化脚本（numpy + Pillow）
├─ tools/                              # 6 个图集/素材脚本
├─ mcp/                                # Aseprite MCP 服务端
│  ├─ src/index.ts                     # 源码（上游 + 本仓库补丁，60 工具）
│  ├─ build/index.js                   # 预编译产物（装到工具包里的就是它）
│  ├─ package.json / package-lock.json # 依赖（npm ci --omit=dev）
│  └─ LICENSE                          # 上游 MIT 许可（原样保留）
└─ scripts/
   ├─ build-kit.mjs                    # 组装 dist/ + manifest + 可选 zip
   ├─ install.ps1 / uninstall.ps1      # 安装 / 卸载
   └─ notices.mjs                      # 重新生成 THIRD-PARTY-NOTICES.md
```

安装后的落地结构（默认 `%USERPROFILE%\.dsh\pixel-toolkit`）：

```
pixel-toolkit/
├─ mcp/{build/index.js, node_modules, ...}
├─ skills/aseprite-pixel-art/
├─ vision/  tools/
└─ tmp/                                 # ASEPRITE_MCP_TMP，运行时临时产物
```

---

## 六、它是怎么接进来的（手工安装 / macOS / Linux）

安装器只做两件事，都可以手工完成：

1. **把 `payload/`（release 内）解到工具包目录**，或把仓库里的 `mcp/ skills/ vision/ tools/`
   复制到 `~/.dsh/pixel-toolkit/`
2. **把 preset 块追加到 profile 补丁** `~/.dsh/profiles/<profile>/cordis.patch.yml`：
   取 `preset/preset-pixel.patch.yml.tpl`，把 `__PIXEL_TOOLKIT__` 换成工具包目录真实路径、
   把 `__ASEPRITE_PATH_LINE__` 换成 `                ASEPRITE_PATH: <你的 Aseprite 路径>`
   （找不到 Aseprite 就换成同样缩进的注释行），整块追加到文件末尾。

关键三行（其余都是常见工具面）：

```yaml
      - id: skill-filesystem            # 这个预设独占自己的技能目录
        name: '@deepseek-ai/dsh-skill-filesystem'
        config:
          customSkillDirs:
            - <工具包>\skills
      - id: mcp-aseprite                # Aseprite MCP（stdio）
        name: '@deepseek-ai/dsh-mcp-client'
        config:
          serverName: aseprite
          command: node
          args: [<工具包>\mcp\build\index.js]
          env:
            ASEPRITE_MCP_TMP: <工具包>\tmp
            ASEPRITE_PATH: <你的 Aseprite.exe>
          transport: stdio
```

> 两个踩过的坑：
> ① preset 的插件面是**整套替换**、不继承 base，所以 `tool-fs-search`（必须写
> `sampleOverCapGlobResults`）与 `tool-todo`（必须写 `allowParallelInProgress`）这类
> **必填 config 必须一起带上**，否则该预设显示「配置失败」；
> ② `@deepseek-ai/dsh-schedule` / `dsh-time-context` / 团队层这些 **Host 级 service provider
> 不要写进 preset**（会二次 `provide` 导致挂载失败）。

---

## 七、排错

| 症状 | 原因 / 处理 |
|---|---|
| 卡片显示「**配置失败**」 | 点卡片里的「查看配置」，把顶部诊断发出来。常见是某个插件行缺必填 config |
| 预设列表里**没有**这张卡 | 没重启 DSH；或补丁写到了另一个 profile（桌面版用 `-Profile desktop` 重装） |
| 有预设，但**没有** `mcp__aseprite__*` 工具 | ① `node -v` 是否 ≥18；② 工具包目录是否被移动（`args` 是绝对路径）；③ 重启后再看 |
| 一画图就报 `Aseprite executable not found` | Aseprite 没装 / 装在非默认位置 → 补 `ASEPRITE_PATH` 后重启 |
| 工具在，但每次生成都超时 | Aseprite 首次启动慢或弹了窗口（试用/更新提示）。手动开一次关掉提示再试 |
| `skill` 列不出来 `aseprite-pixel-art` | `customSkillDirs` 指向的 `<工具包>\skills` 不存在，或层级不对（必须 `skills/<技能名>/SKILL.md`） |
| 安装脚本中文乱码 | 用 `powershell -NoProfile -File ...`；脚本是 UTF-8 BOM，Windows PowerShell 5.1 能正确读 |

安装器失败会**自动回滚**并给出原因；补丁旁边始终留有 `cordis.patch.yml.bak-pixel-<时间戳>` 备份。

---

## 八、升级 / 卸载

- **升级**：换新版后**再跑一次 `install.ps1`**。它检测到自己的标记块就原地替换（会重新备份），
  不会产生第二份 preset。
- **卸载**：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall.ps1                 # 只删 preset
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall.ps1 -RemovePayload  # 连工具包一起删
```

卸载同样先备份、后自校验；做完重启 DSH 才会从列表消失。

---

## 九、开发

```powershell
node scripts/build-kit.mjs          # 组装 dist/pixel-preset-kit
node scripts/build-kit.mjs --zip    # 再压成 dist/pixel-preset-kit.zip
node scripts/notices.mjs            # 重新生成第三方许可清单
```

改 MCP 源码（`mcp/src/index.ts`）后重建产物：

```powershell
cd mcp
npm install                          # 含 devDeps（typescript）
npm run build                        # tsc -> build/index.js
```

构建脚本的闸门：作者机器的路径泄漏、缺文件、ps1 丢 BOM、
占位符被删、依赖没装齐 —— 任一条命中就**拒绝出包**。

---

## 十、已知限制

- **不含 Aseprite 本体**（付费软件），也不含任何 Aseprite 资源/模板。
- 技能里的**示例**来自作者自己的项目（`projects/...` 这类相对路径、若干调色板案例），
  是经验记录；绝对路径已全部改写为工具包内路径。文中少数 `vendor/aseprite-mcp-master/...`
  指作者本机的 MCP 源码树，仓库里对应位置是 `mcp/`。
- 预设 `order: 13`、id 为 `pixel`。若你已有同名预设，安装器会拒绝并提示先删。
- 工具包放 `~/.dsh/pixel-toolkit`（**所有 profile 共用一份**）；想按 profile 隔离就装两份，
  各用 `-ToolkitDir` 指定不同目录。
- 只在 Windows 上验证过安装脚本；macOS / Linux 按第六节手工做（MCP 与技能本身跨平台）。
- `mcp/` 是**打过补丁**的上游版本：上游 43 个工具，这里 60 个（新增 `run_script`、索引色/调色板、
  `verify_artifact` 等，见 `mcp/src/index.ts`）。改动未逐条上游回报。
- 工具箱本身是标准的 stdio MCP 服务端（`mcp/build/index.js`）+ 纯 Markdown 技能，非 DSH 的 MCP
  客户端把路径指过去也能跑；但本仓库只提供并验证 **DSH** 的安装器与 preset，其它客户端的适配不在
  支持范围内。

---

## 十一、许可与致谢

- 本仓库（preset、技能、vision/tools 脚本、安装器）：**MIT**，见 [LICENSE](LICENSE)
- `mcp/`：**aseprite-mcp** by [ayigityol](https://github.com/ayigityol/aseprite-mcp)，MIT，
  本仓库在其基础上打了补丁；上游许可原文保留在 `mcp/LICENSE`
- 第三方 npm 依赖清单：[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
- 不打包但必需：**Aseprite**（商业软件）、**DSH**、**Node.js**、**Python + numpy + Pillow**
