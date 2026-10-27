---
name: aseprite-pixel-art
description: Aseprite 像素画作业规程——新建稿不动既有资产、先定画布与锚点、批量宏一次成图、细线必须像素完美、出图后出放大预览交人验收
---
# Aseprite 像素画作业规程

> **本工具包说明**：文中 `vendor/aseprite-mcp-master/...`、`projects/...` 这类路径指作者本机的源码树与示例项目，
> 本包**不含**这些文件（只产 `mcp/index.js` 编译产物）。遇到「跑某个 selftest / gate 脚本验证」的步骤，
> 改用等价做法：直接让 agent 画一张小图，用 `scripts/check-thin.mjs`（退出码 0 = 无 2×2 块）与 ×8 预览验收。
> MCP 服务端在本包里的位置是 `%USERPROFILE%\.dsh\pixel-toolkit\mcp`。

## 触发
用 Aseprite MCP（工具名形如 `mcp__aseprite__*`，**实测 54 个**；工具面以实测为准，随时复测：
`node vendor\aseprite-mcp-master\_gate-tools.mjs --expect 54`，它比对源码声明/调度表/schema 三者一致；
只想知道"有哪些工具"用 `dump-schemas.mjs`）画像素画、改图、做 spritesheet 时。

## 铁律（按顺序遵守）

1. **只新建，不动既有稿。** 练手与试验一律新建文件，写到本次任务的输出目录。
   **禁止**改写用户既有美术资产（各游戏项目的 `asserts/`、用户指定目录）。要改必须先问。
2. **先定画纸，再谈内容。** 动笔前先明确 **画布尺寸 / 网格锚点 / 边界**（角色画布常用 16·24·32·48·64；图集先定 cell 尺寸）。
   尺寸定错，后面全是返工。**尺寸与锚点由用户拍板，AI 不自行变更。**
3. **AI 不代设计外观。** 造型、配色、构图是用户的决定；AI 负责把它**精确落到像素**，不负责"好看"。
   用户会亲自画布局图/参考图当空间真源，不得擅自增删。
4. **要批量宏，不要小步多轮。** 一张图用**一段 Lua 一次画完**（`run_script`），或一次 `create_sprite` + 若干 `draw_*`。
   忌反复单像素微调、忌同一动作来回试。
5. **细线必须像素完美。** 见下节。
6. **出图必给人看。** 画完导出 PNG，再生成 ×4/×8 放大预览，用 `present` 直接贴给用户。
   **AI 不做审美验收**——只做结构自检（见 `check-thin.mjs`）。

## 标准工序（**前期处理优先**，2026-10-14 定稿 —— 次序的唯一真源）

**先判两件事**：① **有没有参考图/示意图**（有 ⇒ 一切以它为准，不进纯文生图）；② 交付要不要可动/分层。

| # | 步骤 | 闸门 |
|---|---|---|
| 0 | **拿到空间真源**：用户给的示意图/参考图 | **没有参考图不进生成环节**；不擅自增删 |
| 1 | **参考图前期处理**：`ref-analyze.py`（色相分区、地块自相关周期）、`grid-blocks.py`（切块+叠网格+全局坐标） | 周期与网格尺寸对得上 |
| 2 | **规格定死**：画布/网格/锚点/尺寸倍数（`spec/pixel-spec.json`） | **作者尺寸 = 交付尺寸**；⛔ 禁 `resize(BOX)` 出多档 |
| 3 | **调色盘先定**（动笔之前）：`style-profile.py` → `style-palette.py` | 索引 0 留透明槽；**ramp 连续索引**（±1=明暗一阶） |
| 4 | **物件判读表**（先判读再动手）：`object-map.py` → `spec/objects.json` | 每件 px 范围/网格对齐/类别 |
| 5 | **逐件做成"整体单件"**：单品 → 场景摆放；⛔ **禁从成品场景抠物件** | RGBA 透明底 · 尺寸=网格整数倍 · **色板外色=0** |
| 6 | **进场景 / 分层组装** | 层名语义化（禁 `Layer 1`）；回读指纹 + 独立复算 `changed=0` |
| 7 | **光影与抖动**（其次，但绘制时必须做） | 光向**先实测**（禁假设）；抖动默认纯色块 + 机械闸门 |
| 8 | **闸门验收** `verify_artifact` | 量尺先自证；`PASS ≠ 全规格达标` |

> 为什么次序是铁的：2026-10-12 会话把"画风/光影"当主目标、**跳过参考图前期处理**，整条链返工。
> 用户口径：**"画风和光影主要参考图比较重要，请把它们排成其次"**。

### 生成阶段三条硬约束（全部实测）

1. **有参考就必须 img2img（`--ref`）**。纯文生图只是探索手段、不是交付手段——
   没有参考，产物与参考**必然不一致**。
2. **多件物品 = 一张素材表一次生成**（4×3、逐格一件、互不重叠、四周留白）。
   逐件单独生成 ⇒ 风格漂移 + N 倍成本。
3. **⛔ 透明底不可能从生成拿到**（实测：API 返回 `mode=RGB`、alpha 全 255、透明像素 0/1048576；
   `extra_body.background="transparent"` 与 `extra_body.transparent=true` **均 HTTP 400**）
   ⇒ **"提示词里要透明底"是无效指令**。正确做法 = **纯色可抠底 + 确定性去底**：
   提示词只写「背景单一纯色 + 物件四周留白 + 不画投影」；去底用**逐格自身四边 6px 环**估背景色 + 软阈值
   （⛔ 全图边缘估背景色会被暗角毁掉）；白边**先量厚再外侧腐蚀**（腐蚀不可能碰到内部）。

## 像素线条（本 preset 的核心要求）

- **1px 线**：只用 `draw_line` 且 `thickness: 1`。它走 Bresenham 逐像素，天然无 2×2 方块。
- **不要用 `thickness > 1`（实测有据，别按"会报错"理解）**：那条分支走 `app.useTool` 原生笔刷，
  它**不报错**，但产出不是像素线条——32×32 实测：1px 线 0 个 2×2 块 / 2px 线 **26 个**；
  1px 圆 0 个 / 2px 圆 **52 个**。粗笔就是糊。
- **要粗线就自己叠**：2px = 两条 off-by-one 的 1px 线；或用 Lua 显式平移复制。
- **像素完美判据**：画面里**不得存在 2×2 全填块**（斜线拐角不允许双像素）。
  注意：**"同一行两个相邻像素"不是缺陷**——浅斜线每行本来就会有多像素。别按错误定义返工。
- **抗锯齿一律关**：不要用半透明边缘做过渡，用调色板的阶梯色（ramp）。
- **圆/椭圆**：`draw_circle`/`draw_ellipse` 细线也是逐像素；半径 < 3 时手点像素更可控。
  `draw_circle` 的圆心参数是 **`centerX`/`centerY`**（不是 `x`/`y`）——照 inputSchema 传，见下节。

## 形状：按示意图落点 + 逐行显式弧线（2026-10-14 新增）

- **用户的示意图/布局图是空间真源**：形状按示意图落点实现，AI **不自己拟形**。
  示意图里的轮廓、锚点、网格坐标照抄，不擅自增删（与铁律 3 同源）。
- **圆弧不许用"每列取整"生成**：实测 32px 下会出 **3–4px 平顶**——这是"每列取整"的必然结果，
  不是审美问题。正确做法是**逐行显式弧线**（每行 1px 递进），或直接按示意图逐行落点。
- 需要程序化圆弧时，先在目标尺寸下打印每行的起止 x（断言相邻行差 ≤1），再落像素。

## 常用配方

### 起稿
```json
create_sprite { "width": 32, "height": 32, "colorMode": "rgb", "savePath": "<out>/<name>.aseprite" }
```
受限调色板用 `indexed` + `load_palette`；像素画常规是先定板再画。

### 批量宏（首选）
`run_script { "script": "<Lua>" }`，一次画完。要点：
- `local spr = app.open(path)` → `local img = spr.cels[1].image` → `img:putPixel(x, y, Color(255, 0, 64, 255))`
- **`img:getPixel(x, y)` 返回打包整数，不是 Color 对象**
  ⇒ 读颜色必须 `Color(v)`，或用 `app.pixelColor.rgbaR/G/B/A(v)`。
- 返回值就是你 `io.write("__RESULT__" .. json.encode({...}))` 里的对象（服务端已解包，**没有额外信封**）。
- 想省事可直接 `io.write("__RESULT__" .. json.encode({ success = true, data = { ... } }))`，取回的即 `data`。
- 画完 `spr:saveCopyAs(path)`；要同时给 PNG 就再 `saveCopyAs` 一份 `.png`。

### 导出与看图
| 目的 | 调用 |
|---|---|
| 单帧 PNG | `export_frame { filePath, frameNumber, outputPath }` |
| 图集 + JSON | `export_sprite_sheet { filePath, outputImage, dataFile, sheetType, columns, rows }` |
| 分图层导出 | `export_layers { filePath, outputPattern, frameNumber }` |

- 放大预览（给人看）：
  `node "$env:USERPROFILE\.dsh\pixel-toolkit\skills\aseprite-pixel-art\scripts\preview.mjs" <src> <out.png> 8`
- 结构自检（像素线条）：
  `node "$env:USERPROFILE\.dsh\pixel-toolkit\skills\aseprite-pixel-art\scripts\check-thin.mjs" <sprite.aseprite|png>`
  退出码 0=通过，1=有 2×2 块。
- 交付：`present` 贴放大预览 PNG，**不要**只贴原始小图。

### 画稿 ↔ ASCII 网格（`scripts/cel-ascii.mjs`，2026-10-15 新增）

本项目的角色造型**就是以 ASCII 网格存进 spec 文档的**（`projects/farm02/spec/SCALE-AND-CHAR-2026-10-15.md` §5），
所以"改图"常常 = **改字母 → 重新出图**。这条闭环现在有工具，**别再每次手抄 dump / 回写脚本**
（那正是数据形状类 Lua 失败的来源）：

```powershell
$s = "$env:USERPROFILE\.dsh\pixel-toolkit\skills\aseprite-pixel-art\scripts\cel-ascii.mjs"
node $s --selftest        # 纯解析 + 7 条负例，不需要 Aseprite
node $s --selftest-live   # 真起 Aseprite：建夹具 → dump → 改字母 → apply → 回读（含 2 条负例）
node $s dump  <稿.aseprite> --layer 00_base --frame 1 --out grid.txt --legend-out legend.json
node $s dump  <稿.png>
node $s apply <稿.aseprite> --grid grid.txt --out 新稿.aseprite --dry-run   # 先空跑再落盘
```

- **行格式**：前 W 个字符是像素行，之后**必须**跟分隔符（≥ 1 空格）或行尾，其余当注释忽略
  —— 与 spec 文档里"行 + 行号 + 说明"的写法兼容。dump 会自己写出图例注释头，apply 能读回来。
- **三条前置闸门**（不满足就**在动 Aseprite 之前**报错，且**不落任何文件**）：
  ① 每行长度 == 画布宽；② 每个字母必须在图例里；③ INDEXED 稿**板外色 = 0**（等价于闸门 B）。
  另有：混合键表、超 63 色、行数不符都会被拒。
- `dump` 的字母按**首次出现顺序**分配；要跨会话固定字母（便于手改 spec 文档）就传 `--legend legend.json`。
- 它是 `runLua` 的调用方，所以**失败会自动留痕到 `_fail/`**（见「编辑本 preset 脚本的纪律」）。

## 看图回路：把差异量化（`vision/` 原语，2026-10-12 落地）

肉眼估坐标/阈值是返工主因。这三个脚本只依赖 numpy + Pillow（本机无 scipy/torch/onnx），
用**打包 Python** 跑（不是系统 python）：

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py "$env:USERPROFILE\.dsh\pixel-toolkit\vision\image-diff.py"   --a A.png --b B.png --include-alpha
& $py "$env:USERPROFILE\.dsh\pixel-toolkit\vision\region-stats.py" --image P.png --fringe-white --roi 0,0,575,575
& $py "$env:USERPROFILE\.dsh\pixel-toolkit\vision\histogram.py"    --image P.png --palette scene.gpl --union 16
```

- **`image-diff`**：出 `*_heat.png`（热图）/ `*_overlay.png`（差异烧在压暗底图上）/ `*_mask.png` + JSON
  （逐区域 area / bbox / centroid / 幅度）。**纯 alpha 改动必须加 `--include-alpha`**，否则 RGB 相同 ⇒ 误报"零差异"。
- **`region-stats`**：面积 / bbox / 质心 / **边界坐标（`*_edges.txt`）** / 白边厚度；`--roi x0,y0,x1,y1` 逐格处理
  素材表（输出坐标是**原图绝对坐标**）；`--crop-out --soft-alpha` 等价于"纯色底软阈值抠图"。
- **`histogram`**：调色板映射误差（mean/p95/max）+ 覆盖率；`--union N` 一步出并集板
  （道具硬套场景色板会差一个数量级：实测 mean 56.1 → 9.8）。
- 改动后先跑自检：`selftest-vision.py`（51 断言）/ `verify-vision.py`（14 条真产物断言，含两路互校）。
- 两条静默失效的坑：**别按 bbox 裁切算"到透明区的层距"**（层距全空 ⇒ 白边永远测成 0px）；
  **别用全图边缘估背景色**（暗角会让背景变成一整块前景：2K 素材表实测多出 1 个整图 bbox 的区域）。

## 环境（本包给出的默认值）

- Aseprite：由 `ASEPRITE_PATH` 指定，或放在常见安装位置（`C:\Program Files\Aseprite`、Steam 库、`%LOCALAPPDATA%\Programs\Aseprite`）由服务端自动探测。
- MCP 服务端：`$env:USERPROFILE\.dsh\pixel-toolkit\mcp`（本包只含编译产物 `index.js` 与运行时依赖）。
  要改 MCP 源码：另取上游 `aseprite-mcp` 源码，构建后用新的 `build/index.js` 覆盖该目录的 `index.js`。
- 产物目录约定：`当前会话工作区\out\`

## 开工检查表（写脚本 / 写 Lua / 写闸门前先读 —— 2026-10-15 定稿）

> 来源：farm02 一轮里 14 次失败（6 次报错 + **8 次静默错**）的方案级复盘，
> 全文见 `projects/farm02/spec/RETRO-2026-10-15.md` 与 `spec/HANDOVER-TOOLING-2026-10-15.md`。
> 元根因一句话：**每写一个新脚本，就把"可复用的约定"重新决定一遍** ⇒
> 同一类错误在不同文件里各犯一次（数据形状 4 次 / 图像前提 2 次 / 无统一入口 5 次）。
> **14 次里没有一次是环境坏了。** 最危险的不是崩掉的，是**静默错的**（不报错，只给出错数据）。

**写任何脚本前：**
- 开头**断言输入存在**（Python `os.path.exists` / Lua `app.fs` 校验）；**路径不要手打**。
- 处理**生图裁切件**：第一步就**剔背景**（全图众数色 ±70），再做量化/切分/度量。
  ⚠ 众数色**不是**边框色——生图带白框时按边框取背景会整套取错。
- 字符串 `split` 之后**一律 `strip()`**。

**写 Lua 前：**
- 表**用具名字段**（`p.idx`），**不用位置下标**（`p[5]`）——下标写错时，报错行号指向**受害者**
  （常常是 20–30 行外的 `px()`/`toIndexedSprite`），真凶在赋值处。
- **算子只吃单层，`build` 负责三层**：把这条写进函数签名上方。
- 两条断言当场值钱的：**每行长度 == 画布宽**、**每个字母必须在 LEGEND 里**。
- API 存在性**先实测**，别靠印象（见下节「坑（实测）」：
  `sprite.palettes[1].size` 这种不存在的东西，**一次会话里踩了两次**）。
- 走 `node scripts/cel-ascii.mjs dump|apply` 的"画稿 ↔ ASCII 网格"闭环，
  别再每次手抄一遍 dump/回写脚本（**它自带行长断言、图例断言、以及 INDEXED 下的板外色闸门**）。

**写闸门前：**
- **先量基准再定阈值**：凡 `=0` / `>X` 的判据，先跑一遍打印实际值
  （曾有判据写"逐帧 =0"，而基准本来就是 5 ⇒ 假失败）。
- **正例自己必须满足基本不变量**（连通/单色/行长），否则负例试牙会**假失败**。
- 每条闸门配一个**"能把它打挂"的负例**；`--selftest` 全绿才准去验产物。
- 锚点判据按动作分模式：idle = `rows`（脚下冻结）、walk = `bottom`（地面线恒定）。

**交人验收前：四向/持物语义检查单（2026-10-16 加，血泪：一晚被你抓 4 次，闸门全绿也拦不住）**

> 闸门只能证明**结构**（连通/厚度/锚点/镜像关系），证明不了**语义**。
> 下面 4 条是 2026-10-15 那一晚**全部由用户眼睛发现**的，每条都曾"闸门 PASS"。

- [ ] **持手**：同一动作四个方向必须是同一只手。对照表：**南=持物在画面左 · 北=画面右 · 东=身前近侧 · 西=远侧（手臂被身体挡住）**。
      正面画错手性 = 一眼可见的"左撇子"；**profile 看不出手性，必须用南/北验**。
- [ ] **道具与手的间距**："提着" vs "远处捏着"：道具顶端要**与手同高**（实测两次都是"吊太低"被指像拿着壶盖）。
- [ ] **剪影能不能读出动作**：单看剪影（填黑）能不能说出"他在干什么"；读不出就调整幅度/方向。
- [ ] **四向对照图先出、再精修**：同一动作四向并排贴给用户看，**不要**一个方向精修到底才拼图（返工面差好几倍）。

**跨脚本复用的三条底层约定（每条都因"每个脚本重新决定一次"而翻车过）**

- **描边不叠两排/两列**：描边前先取快照；目标格若**下方**或**左右**已是轮廓则跳过（否则 2×2 黑块）。
- **细线要 4 连通**：1px Bresenham 的对角步是 8 连通 ⇒ 连通判据（4 邻域）会判成多块；**对角步补 L 型角像素**。
- **细线不描边**：1px 线再描边就是 3px（既不"细"、又会贴上既有轮廓）⇒ **只有实心件才描边**。

## 坑（实测）

**Aseprite Lua API（2026-10-15 farm02 劳作动作那一轮全部实测）**

- **`Sprite` 上没有 `cel()` 方法**（`Field cel does not exist`）。**未知字段直接抛错**（`__generic_mt_index`）
  ⇒ 连 `type(spr.cel)` 都不安全，探针要 `pcall` 包起来。取"某层某帧"的 cel 只能**扫 `spr.cels`**
  比对 `c.layer` / `c.frameNumber`（cel 有 `image` / `position` / `bounds`）。
- **`frames[i].duration` 单位是秒**（实测 `0.1` = 100ms；写 `123` 被夹到 `65.535`）。110ms ⇒ 写 `0.11`。
- **`saveCopyAs` 派生的副本会继承源稿的 tag**（实测副本 `tags=2`）⇒ 换动作前必须**先删旧 tag 再建新的**，
  否则留下一个名字不对的 tag（`deleteTag` 不存在时用改名兜底）。
- **自己的写入函数，参数顺序要当场核对一次**：本轮 `put(f,l,x,y,v)` 被写成 `put(f,1,24,x,…)`
  （把 x 位填成 24）⇒ 悄悄在 (24,16..19) 留下 2 个孤立像素 ⇒ **每帧 2 连通块**。
  这类错不报错、只让闸门红，`(24,16)` 这种坐标一看就知道是"把常量填错了位置"。

**像素线的"粗"有三种来源，第三种最容易被自己造出来（2026-10-15 实测）**

- ASCII 网格里**自带轮廓 + 再让自动描边描一圈** ⇒ 相邻两排轮廓 ⇒ **2×2 黑块 7~10 个**（基准 0）。
  **正确做法：网格只放材质，轮廓一律由描边生成。**
- 自动描边与**既有轮廓列相邻**也会出 2×2（本轮：壶的轮廓列贴上腿/靴的既有轮廓列 x19/x20）
  ⇒ 洞见：**描边目标要离既有轮廓 ≥ 1px 空格**，必要时整体前移 1px。
- 手臂下缘轮廓正好压在躯干底轮廓上（相邻两行全黑）⇒ 也出 2×2；本轮把那一帧改成**水平回弹**即解。

**闸门自身的过期（假失败）—— 报 FAIL 先怀疑量尺**

- **`.gpl` 会过期**：改过板色之后（例：换肤去饱和），旧 `.gpl` 少了新色、多了旧色 ⇒ 闸门"板外色=0"
  从此对任何新产物都 FAIL，而报出来的"板外色"**全是真板色**。判板外色要用**从产物自己导出的板**
  （本项目 `tools/export-gpl.lua`：pcall 逐索引试探板容量，因为 Palette 无 size 访问器）。
- **`preview.mjs` 曾对 INDEXED 稿输出全透明图却报成功**（已修）：它用 `Color(srcImg:getPixel(x,y))`，
  而索引模式下 `getPixel` 返回**调色板索引**，`Color(索引)` 的 alpha 字节为 0 ⇒ 每像素判透明。
  修法两条：① INDEXED 感知读像素（`sprite.transparentColor` + `palettes[1]:getColor`）；
  ② 加闸门 **输出不透明数 == 输入 × scale²**，不符就不落盘并报错；`--legacy-read` 是它的负例。
  **教训**：放大/导出这类"看着必然成功"的工具，也要有一条能把它打挂的判据。

- **判"轮廓增厚"的闸门必须用对轮廓色，且要先自证找到了目标**（2026-10-15 实测，farm02）：
  `verify-idle.py` 的闸门 F 把轮廓色写死成 `#131313`，而**角色板的轮廓是 `#171310`**
  ⇒ 该闸门对角色稿**永远报 0 块 = 空转的假绿**（此前多份 spec 记的"基准 0"全无效）。
  已修：加 `--outline #rrggbb` + **自证**——画面里找不到该颜色时判 **"量尺无效 ⇒ 拒绝出结论"**
  （负对照实测：传错色 → FAIL + 明确原因，而不是 PASS）。用对色后真实基准 = **5 块/帧**
  （源稿腿间那道 2px 描边，属既有美术选择）。
  **通用规则**：任何"数某种颜色的块"的判据，都要先断言**该颜色在产物里真的存在**。

- **镜像派生：别用"按坐标区域判是不是肢体"的写法**（2026-10-15 实测两次翻车）：
  ① 按**目标坐标**判区域 ⇒ 镜像后落在另一侧的肢体（如手里的道具）永远搬不过去；
  ② 改按**源坐标**判 ⇒ 又漏掉"有道具之后该行不再左右对称"，正面的道具被留在原侧。
  **稳的写法是两步式**：`先整幅镜像 → 再把"该换的衣服行/躯干列"按坐标区间替换`
  （镜像管几何，替换管衣服），并配两条自检：**剪影 == 对面镜像**、**不该动的行逐像素没动**。

- **持物角度（手性）必须在正面/背面视角上验一次**（2026-10-15 用户当场抓到）：
  **面向镜头（南）时，角色的右手在画面左侧**（跟握手一样）。照着侧面的"手臂在屏幕右侧"画正面，
  就会画成**左手持物**——而 **profile（侧视）是看不出手性的**，只有正面/背面能验出来。
  四向同一动作的手性对照（持物在画面哪侧）：南=**左** · 北=**右** · 东=身前近侧 · 西=**远侧**（最难，常被忽略）。
  ⇒ 定动作前先写死"哪只手拿"，并在南/北两向各验一次。

- **西向（持物在背对镜头那一侧）不能靠镜像**（2026-10-15 用户口径：
  「**如果是右手，west 是看不到手肘拿着的**」）：
  角色面向西时右手在**远侧** ⇒ **手臂/手肘被身体挡住，只看得到道具**。
  整体镜像只会把道具换到近侧（= 左手），是错的。稳的写法是**按格子分区取值**：
  ```
  y<=18            → 取本向 idle（头/肩/帽）
  y>=19 且 x>=11   → 取本向 idle（躯干 —— 把近侧手臂"藏到身体后面"）
  y>=19 且 x<=10   → 取对面（东）的镜像（道具 + 提手黑缝 + 露出的一截手）
  ```
  自检：**身体区逐像素 == 本向 idle**、**道具区逐像素 == 对面镜像**、剪影单连通、地面线恒定。

**`sprite:close()` 会销毁它的 Image（2026-10-15 实测）**

- 先存 cel 的 `Image` 引用、再 `sprite:close()`，之后访问报 **`Tried to access a deleted 'ImageObj'`**。
  ⇒ 关精灵之前必须把像素读成普通 Lua 表（或干脆不关）。跨精灵搬像素时先全部读入内存再写。

**"左右朝向"派生：几何镜像 + 光影留世界坐标（规则要逐像素反推，别照文档复述）**

- 做法（在既有 e/w 稿上量出来、并在 9 个差异像素上验证）：
  `base_west(x,y) = base_east(31-x, y)`（实测镜像匹配 **1024/1024**）；
  `shadow/highlight` **位置原样沿用东向 (x,y)**（光留世界坐标），但**值换成"镜像后该处材质"的
  阴影阶/高光阶** —— 因为衣服不对称，镜像后底下的材质换了（实测 9 处差异全部吻合：
  衬衫阴影→背带裤阴影、衬衫高光→皮革高光 …）。
- **新增边界（动作姿势才会触发）**：若东向有光影像素、而镜像后该处**底下没有材质**（透明/描边），
  **必须丢掉该像素** —— 否则得到"漂浮在空白上的光影像素"，剪影镜像闸门也会挂。
- **例外：手里的道具/农具，它的光影要跟着它自己镜像**（`x → 31-x`）。理由是可验证的：
  **水平镜像下"顶缘"仍映射到"顶缘"**（y 不变），所以道具顶缘的受光高光跟着镜像才对；
  若也按"位置不动"处理，前伸的道具镜像后高光会落到空白格上 ⇒ 被上一条丢掉 ⇒ 道具变成一团死灰
  （farm02 实测：水壶亮金属像素 东 5 → 西 0，用户当场看出"亮的像素点不见了"）。
  修法只需按材质判定：道具独占的材质（如金属）走镜像分支，身体走"位置不动"分支。
- ⚠ **别想当然用"通用光照规则"重算既有高光**：我试过「高光 = 材质的上缘或左缘」，
  在**东向自己身上验证就挂了**（命中 152 / 漏 180 / 多 104）⇒ 高光是逐处手放的。
  **规则要先在"有 ground truth 的那一侧"复现成功，才准拿去改另一侧**，否则等于替用户重画光影。
- ⚠ **判据要跟着姿势换**：「逐行内容中心 = 15.5」只在**左右对称的剪影**（站立 idle）上成立；
  **手臂前伸的动作姿势下东向自己就偏离**（实测每帧 5~6 行）⇒ 该判据会被误当产物缺陷。
  动作姿势的正确判据是 **`w_center(y) == 31 - e_center(y)`**（实测违例 0）。

- **`getPixel` 返回整数**（见上），直接当 Color 用会报 `attempt to index a number value`。
- **参数名必须照 inputSchema 传**（例：`draw_circle` 是 `centerX`/`centerY`，不是 `x`/`y`）。
  服务端已在调度层按各工具 `inputSchema.required` 做必填校验，缺参会直接返回可读错误：
  `Missing required parameter(s) for "draw_circle": centerX, centerY. Accepted parameters: ...`
  —— 看到这条就照 Accepted parameters 改，别去猜 Lua 里的算术报错。
- **`thickness > 1` 不报错、但也不是像素线条**（实测 26/52 个 2×2 块）。
  别把它误判成"batch 不支持 useTool"——它跑得动，只是糊。
- **`get_aseprite_version`** 走 Lua 读 `tostring(app.version)`（CLI `--version` 走 stdout，本机 stdio 被 ignore）。
- **沙箱内 node 不能用 piped stdio 起子进程（EPERM）**：本服务已改成 `stdio:"ignore"` + 文件结果通道；
  本技能脚本用同一套路，别改成 `execFile`/`execSync`。
- **`set_palette_colors` 有顺序要求**（实测）：条目形状是 **`{index, r, g, b, a}`**（不是 `{index, color:"#hex"}`），
  且**必须先 `resize_palette {newSize:N}` 腾出位置**——RGB 稿初始调色板只有 1 项，
  直接设 `index >= 1` 会报 `index out of bounds`。常用序：`resize_palette` → `set_palette_colors`（或直接 `load_palette`）。
- `os.tmpdir()` 可被 `ASEPRITE_MCP_TMP` 覆盖（宿主限制临时目录写入时用）。
- **`sprite.palette` 不存在** ⇒ 用 **`sprite.palettes[1]`**（实测报 `Field palette does not exist`）。
- **`Palette` 上没有容量访问器**（2026-10-15 实测）：`size` / `length` / `count` / `getSize` /
  `numColors` / `getColors` **全都不存在**，只有 `getColor` / `setColor` / `resize` / `frame`。
  ⇒ **板容量只能自己记**（`resize(n)` 之后越界才报错，`index out of bounds`）。
  同一条假设**一次会话内踩了两次** ⇒ 别靠印象，见本节就用这两条。
- **`Color` 的字段是 `red` / `green` / `blue` / `alpha`**，**不是** `r`/`g`/`b`/`a`
  （`Color{r=…}` 会得到默认值，静默画错色）。
- **`spr:crop` 只改画布、不销毁 cel 数据**（2026-10-15 实测，是最贵的一个坑）：
  "裁小再补回网格倍数"等于**白裁**（实测 `shed_scrap` 裁后内容像素 **27210 → 27210**，一个没变；
  `greenhouse_se` 画布已 128×96 而内容 bbox 仍 124×162）。
  真裁必须**逐层重建** `cel.image = Image(w,h)` + `cel.position = Point(x,y)`，
  且读旧 cel 时**按 `cel.bounds` 换算坐标**（忘换算只捞回 300/1200 像素）。
  ⇒ MCP 侧已封装成 `crop_cels`；手写时才需要自己重建。
- **`spr:resize` 会缩放内容**（64×64 的 cel 变 32×32）⇒ **改画布只能用 `spr:crop`**。
- ⛔ **`Image:drawImage()` 的负偏移与 3 参 `(srcRect, dstRect)` 形式都不按预期取样**：
  裁剪/搬移路径改为**手工逐像素拷贝重叠区**（1024×576×3 层裁到 512×288 实测 **0.10 s**，够快）。
- **`json.encode` 遇"混键表"抛 `invalid key to 'next'`**（2026-10-15 定位）：同一张表里既有
  整数键 `1..n` 又有字符串键（如 `{a, b, c, d, cx = …}`）就炸。
  **规则：要 `json.encode` 的表，键要么全是字符串，要么是纯数组**——两层混用是最常见的形态。
  纯字符串键后错误立刻消失（原现场已因脚本被删而不可追，见下条）。
- **失败即删脚本是上游 MCP 的行为**（`vendor/aseprite-mcp-master/src/index.ts` 的 `finally` 里
  `unlink(tmpFile)`）⇒ `run_script` 报错后**现场就没了**，只能靠行号反推。
  tips：本技能的 `scripts/aseprite-lua.mjs` **已改成失败留痕**（见「编辑本 preset 脚本的纪律」）；
  走 MCP `run_script` 时想留现场，就**先把脚本写成一个文件**（如 `aseprite/_fail/<名>.lua`）再 `dofile` 它。

## 调色板（索引色是像素画的真源，2026-10-14 实测补全）

- **新建 INDEXED 稿的调色板默认 256 项**（不是 1 项）；`pal:resize(n)` 之后才是 n 项。
  条目形状 `{index, r, g, b, a}`，**先 resize 再 setColor**，否则 `index out of bounds`。
- **`spr.transparentColor` 默认 0** —— 索引 0 是**透明槽**，不能当颜色用。
  **判透明只能看 `index == spr.transparentColor`，不要读 `pal:getColor(0).alpha`**（那是 255）。
  实测：按 palette alpha 自检报 `opaque=3072`（整幅画布），成图 PNG 实际只有 **1257**（= 圆面积）。
- **索引模式下 `img:getPixel(x,y)` 返回调色板索引**（不是打包 RGB）；`img:putPixel(x, y, 7)` 传**整数索引**可用。
- **换调色板 = 换全部颜色，零像素改动**（实测三层索引指纹换板前后**完全一致**）
  ⇒ 这就是"光影分层 + 引擎 ramp 换色"（C1）的正确机制；**不要**用 Multiply/Screen 半透明去叠光影。
- **换肤的前提是 INDEXED，且每个材质占自己的连续索引**（2026-10-15 实测，NPC 变体 6 套）：
  **RGB 稿换板无效**（颜色已烘进像素）；板子若把"眼睛"和皮肤并到同一段 ramp，改肤色会把眼睛
  一起带成红色并**留下红轮廓** ⇒ **眼睛必须独占索引**。
  同样地：衬衫 / 背带裤 / 皮革 / 金属**各自连续**，就能独立换 ⇒ 变体数相乘而像素只有一套。
  (`tools` 侧批处理：`tools/palette-desat.py` / `tools/skin-variants.py`)
- **改色优先于重画**：只改既有像素的**颜色**时 alpha 逐像素不变 ⇒ 形状零风险
  （farm02 实测 5 件光伏 + 2 处屋顶共 **2.7 万像素**改动，alpha 掩膜逐像素 diff = 0）。
  能用"改板 / 色表重映射"解决的，不要重画形状。
- **ramp 连续索引**：索引 ±1 = 明暗一阶。板子必须**按 ramp 组织**，否则换阶出来是斜条带而不是体积
  （median-cut 排序的板子相邻索引之间没有明暗关系）。
- **blendMode 读写是数字**：`NORMAL/SRC_OVER=3`、`MULTIPLY=14`、`SCREEN=15`（读写往返已验证）。
- 工具（`$env:USERPROFILE\.dsh\pixel-toolkit\vision\`，均带 `--selftest`）：
  `palette-forge.py`（重铸 ramp 板）、`index-annotate.py`（**标注取色→色号**）、
  `style-profile.py` / `style-palette.py` / `palette-edit.py`（风格→板，见下节）；
  Aseprite 侧回读断言用 `aseprite/verify-palette.lua`。

## 风格设置：参考图 → 调色盘（2026-10-14 新增）

**风格 = 可复算的数字，不是形容词。** 三个工具（均带 `--selftest`，全 24/24 过）：
`style-profile.py`（参考图 → 风格画像）· `style-palette.py`（画像 → 板）· `palette-edit.py`（改板）。

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
# 1) 参考图 -> 风格画像（色相族 / 彩度包络 / 明度阶 / 抖动 / 中性占比）
& $py vision\style-profile.py --image ref.png --name myref `
      --out out\style\myref.style.json --swatch out\style\myref.style.png
# 2) 画像 -> 调色盘（ramp 连续索引；可带目标图做内容色并集）
& $py vision\style-palette.py --profile out\style\myref.style.json --steps 4 --neutral 3 `
      --image target.png --union 8 --out-gpl out\style\myref.gpl --out-swatch out\style\myref.swatch.png
# 3) 改既有板（操作可组合、按顺序生效、逐索引出变更报告）
& $py vision\palette-edit.py --in A.gpl --out B.gpl --report B.report.json `
      --set 3=#ff0000 --sat-scale 1.2 --lock 0,1 --rampize
& $py vision\palette-edit.py --in A.gpl --out B.gpl --remap-to-profile ref.style.json --style-strength 0.7
```

铁律（全部实测）：
- **索引 0 是 Aseprite 的 `transparentColor` 槽**，不能当颜色用 ⇒ `style-palette` 默认在索引 0 放占位，
  真实色从 1 开始（`--no-transparent-slot` 只在产物不进 Aseprite 时用）。
- **板必须按 ramp 连续索引组织**（±1 = 明暗一阶）；`--rampize` 把乱序板重排成"中性在前 + 族内按 L\* 升序"。
- **中性结构色（C\* < 5）不参与风格曲线**：实测 `complex_tilemap_16px.png` 的纯黑 C\*=0 ⇒ 色相角 0°
  会被算成 "red" 族并占 61.6%，把 `dark/peak` 压成 0.000，整条曲线失真。工具按 `--c-min` 分离，
  中性单独报（`neutral.share`）。
- **峰值必须有显著性才算"风格曲线"**：无下限时 8bit 量化噪声的 argmax 有 ~50% 概率落在中间 ⇒
  假"彩度峰值在交界带"。`peak_prominence < 1.15` 判为 `chroma-peak:flat`。
- **"按参考风格改板"只动彩度、保留色相构成**；参考图**没有**的色相族一律不动（不凭空发明风格）。
  `--style-strength 0` = 完全不改，可当负对照。
- **互校证据**：`style-profile` 从 `out/single/mushroom_center_red.png` 独立量出
  **peak L\*45.0 / C\*71.4 / dark_over_peak 0.569**，与 `spec/material-light-templates.json`
  手工实测的 `cap_red: L43.4 / C72.45 / dark 0.574` 吻合（两条独立路径互校）。
- **抖动是风格参数，默认低**：像素化前先读画像的 `dither` 值，默认纯色块，
  只在**指定 bbox 与指定 ramp 阶之间**抖动（见「抖动纪律」）。
- 画像/板都是 UTF-8 JSON + `.gpl`，可当**跨会话续接的项目规格**（避免换会话就断）。

## Aseprite 原生原语（2026-10-14 新增 6 个，工具总数 43 → 49）

此前掩膜/图层/调色板操作全被迫"导出 → Python → 导回"。现在原生可用：

| 工具 | 作用 |
|---|---|
| `indexify` | RGB → INDEXED（**先 ColorQuantization 再 ChangePixelFormat**，见坑） |
| `get_layer_image` / `set_layer_image` | 图层 cel 图像读写往返（PNG） |
| `snap_to_palette` | 把每个像素吸附到板内最近色（唯一色缓存；**跳过透明槽**） |
| `remap_palette` | 载入 `.gpl` 等板 + 可选重映射像素 |
| `apply_mask` | 只在 mask PNG（白且不透明 = 内）里编辑：`fill` / `clear` / `keep` |

实测铁律（完整矩阵见 `aseprite/_probe/PRIMITIVES.md`）：

- ⛔ **`ChangePixelFormat{format="indexed"}` 单独用会毁图**：实测 4 色稿 →
  `colorMode=INDEXED` 但 `paletteSize=1`、唯一色 **4→1**（静默塌陷），且 `colorCount` 参数无效。
  必用顺序：**`ColorQuantization{maxColors=N}` → `ChangePixelFormat{format="indexed"}`**。
  ⚠ **只看 `colorMode==INDEXED` 会把塌陷判成成功**——必须数唯一色。
- ⛔ **`maxColors` 必须严格大于图像唯一色数**（索引 0 是透明槽，吃掉一个板位）：
  实测 4 色稿 `maxColors=4` → 塌成 1 色；`5/8/16/256` → 保住 4 色。工具已自动 +1。
- ⛔ 三条探测前提修正：**`app.command[不存在的名字]` 会抛错**（不是返回 nil）⇒ 存在性检查用 `pcall`；
  **`app.command` 是 userdata，不能 `pairs` 遍历**；**`Image`/`Palette`/`Sprite` 是构造函数不是类表**
  （`Image.saveAs` 报 "attempt to index a function value"）⇒ 方法存在性只能在**实例**上测。
- 该版本**不存在**的：`ApplyMask` / `NewMask` / `SetPaletteEntry` / `ChangeColorMode`。
  掩膜用逐像素实现；设板条目用 `pal:setColor(i, Color)`。
- ⚠ **本表别再靠印象判断"有没有"**：曾有一场会话判 `get_layer_image`/`set_layer_image`/`verify_artifact` 缺失，
  于是绕道 `app.open(png)` + Python 往返；实测三处一致（`src/index.ts` 有定义+handler 注册、
  `build/index.js` 有、`tools/list` 有）——**它们都在**。怀疑缺工具先跑 `dump-schemas.mjs` 复测，再决定绕不绕。
- **索引 0 = 透明槽的下游影响**：给 4 色板，实际只有 **3 色可用**（实测 `snapped.png` 只剩蓝/绿/黄）。
  `style-palette.py` 默认在索引 0 留占位正是为此。
- 验证入口：`vendor/aseprite-mcp-master/selftest-primitives.mjs`（**26/26**，走真实 MCP 层），
  另有两条独立路径交叉：`image-diff` 实测掩膜改动 **128/256、bbox 正好左半幅**；
  PIL 直读 `snapped.png` 的唯一色**恰好等于板内可用色**。

## 闸门机制化：`verify_artifact`（2026-10-14，工具数 49 → 50）

**落了一半**：闸门**引擎**已进工具（不再靠临场拼命令），**自动触发**仍缺（见末尾）。

工具行为：读 `spec/pixel-spec.json` 的**可执行**闸门 → **先验量尺自身**（`discipline-metrics --selftest`，
不过就拒绝出结论）→ 再验产物 → 写 `<artifact>.verify.json` → 返回逐闸门 PASS/FAIL。

- 已覆盖（机器可判）：`alpha` 二值性、`dither` 棋盘格 ≤3% / 椒盐 ≤8%、
  `palette` 板外色 = 0（**传了 `--palettePath` 才启用**）。
- **未覆盖**：返回里显式列 `gates_not_machine_checkable`（per_tool_selftest / metric_consistency /
  layered_artifact / no_new_colors / silhouette）⇒ **`PASS` ≠ 全规格达标**，这个字段就是防这种误读。
- **量尺无效必须拒绝**：换成一个 `--selftest` 会失败的量尺 → 返回 `CHECKER_INVALID` +
  `REFUSED: the checker failed its own --selftest`，且**不写 sidecar**（否则等于用坏仪器给产物盖章）。
- 实测正/负对照：好产物 `PASS`；全局 Bayer 底板 **`FAIL`**（checker **9.40%** / isolated **43.91%**，
  双超限）；坏量尺 → `CHECKER_INVALID` 且磁盘 `sidecar_count=0`。

调用（**避开 pwsh 引号坑**）：pwsh 把裸双引号剥掉再交给 native 程序，`'{ "a": "b" }'` 到 node 变成
`{a:b}` 直接 JSON 解析失败 ⇒ 一律走**参数文件**：

```powershell
node vendor\aseprite-mcp-master\_call-tool.mjs verify_artifact "@args.json"
```

**仍需重启 GUI 的部分**：`tools/post-execute` 钩子（产物落盘即自动验证、缺闸门自动标"未验证"）。
那需要写 DSH 插件 + 改 `profiles/web` 依赖 + 重启 dsh web，会中断当前窗口 ⇒ 待用户定时机。
在那之前，**闸门仍要靠"我调用它"**——但至少量尺无效时会拒绝出结论。

## 图集合成两件套（`atlas-pack.py` + `atlas-check.py`，2026-10-14）

交付"资产包 / 合集"时用（工序 6/8 之后的打包交付）。**打包与验收分成两个文件**——验收只吃产物，
不读打包脚本的内部状态：

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
# 1) 打包（与项目无关，任何素材目录都能用）
& $py "$env:USERPROFILE\.dsh\pixel-toolkit\tools\atlas-pack.py" --src <素材目录> --pattern "hand_*.png" --tile 32 --maxw 1024 --out <名>
#    --tile 16 换格子；--cols 8 固定每行 8 件（方阵排列）；--no-lua 只要 PNG+JSON
# 2) 验收（独立闸门）
& $py "$env:USERPROFILE\.dsh\pixel-toolkit\tools\atlas-check.py" --index <out>.json --expect-ids <清单或已有索引.json>
& $py "$env:USERPROFILE\.dsh\pixel-toolkit\tools\atlas-check.py" --selftest    # 先用它自证有牙
```

- 产物三件套：`<名>.png` 合集 + `<名>.json` 坐标索引（`items[]` 含 `id/x/y/w/h/gw/gh/file`，引擎按它切图）
  + `<名>.lua`（`dofile` 即组装成"每件一层、层名=件名"的 `.aseprite`）。
- 闸门 8 条：非空 / 画布为 tile 整数倍 / 格子对齐 / 不越界 / 不重叠 / **逐件像素与源图完全一致** /
  格外无杂散墨 / **选件集逐 id 相等**（`--expect-ids`）。
- ⛔ **实测坑（会静默通过）**：素材目录里混着质检图/演示图时（本次 `hand_strip_a/b/c`、`hand_strip_tank`、
  `hand_tank_x5`、`hand_tank_outline` 共 6 张），打出的 58 件合集**前 7 条闸门全绿**——
  它们只证明"合集忠实复制了所选件"，管不了"选件本身错"。
  ⇒ **交付前必带 `--expect-ids`**；取件优先 `--ids-file` 点名，别靠 `--exclude` 排除词猜。
- 自检两条纪律：**每条闸门都要配一个"它能被触发"的负例**（夹具若被占格矩形铺满，
  "格外无杂散墨"这条永远挂不了，等于没有）；`--expect-ids` 两侧都要非空才可比。
- 实测（farm02，52 件）：`--tile 32 --maxw 1024` → **992×1152**；`dofile` → **52 层、failed=[]**；
  `atlas-check.py --selftest` 7 用例全过。

## 光影（分层与统一）

- **远景/静态底板可以烘焙**；**可交互动产必须走 C1**：固有色层 + 阴影层 + 高光层，三层都是索引色 + 二值 alpha。
- **投影/接触影/环境光遮蔽不许烘进资产**：它们与位置绑定，抠出来单独看必然错
  （实测 12 件道具接地影比 0.34–1.16、光照方向 8 左 / 2 右 / 2 中性，全是"别人家影子"）。
- **光源必须在规格阶段定死**（本 preset 默认 **NW 左上**），ramp 4 阶（暗/中暗/中/亮）。
- 度量光影是否统一：**R/L（右/左亮度比）、B/T、foot（底部 12% 接地暗带）**——统一光源后 12 件应同向。
- ~~`relight-split.py`~~ **已退役**（文件移入 `aseprite/_retired/`）：它当年能把 12 件方向从
  8 左/2 右/2 中性校到 **11 左/0 右/1 中性**，但逐像素重映射把光影**烧进物品像素**、
  且受该材质 ramp 长度限制。新活一律走 `light-rig.py` 分层绘制，**别再用它**。

## 抖动纪律

- 抖动是"色板不足时的最后手段"，**不是默认柔化手段**。默认一律纯色块。
- 只在**指定 bbox 与指定 ramp 阶之间**抖动；闸门卡：棋盘格单元 ≤3%（大平面 ≤1%）、椒盐像素 ≤8%。
- 实测反例：全局 Bayer4 用在底板上 ⇒ 棋盘格 **13.21%**、椒盐 52.67%，即"无脑算法噪声"。

## 编辑本 preset 脚本的纪律

- **⛔ 含中文的 `.py`/`.lua` 禁止用 PowerShell `Get-Content`/`Set-Content` 往返编辑**：
  PS5.1 按 ANSI 写回 ⇒ 文件既非 UTF-8 也非 GBK，Python 报 `Non-UTF-8`，
  `read`/`write` 工具也打不开（`write` 还会被版本守卫挡"file changed since it was read"）⇒ 只能删除重建。
  改这类文件**只用 edit/write 工具或 Python**。
- **量尺先自证**：每个检查器都要有 `--selftest`（合成样例 + 已知答案），否则"永远通过"和"永远失败"都是假闸门。
- ⛔ **禁 `python -c` / `node -e` 承载带引号的代码**（2026-10-16 一轮里同一族踩了 **3 次**）：
  PS5.1 把裸双引号剥掉再交给 native 程序 ⇒ `-c 'out = r"D:\..."'` 到 Python 变成 `out = rD:\...`
  直接 SyntaxError；用 here-string `@"..."@` 时又可能整个片段被吞、**静默无输出**。
  ⇒ **一律先把代码落成 `.py` 文件再跑**（回荡用 `Set-Content` 写 .py 也只在**纯 ASCII** 时可用）。
- ⛔ **跑任何 `scripts/*.mjs` 前先看它的头部**：本技能的 `scripts/aseprite-lua.mjs` 是**库不是 CLI**
  （只 `export`，无入口）⇒ `node <它> <x.lua>` 会**退 0 且什么都不做**（最坏的一种失败：看起来成功）。
  跑 Lua 文件用 **MCP `run_script` + `dofile("<路径>")`**。
- ⛔ **对"文件格式 / API 语义常量"的断言，也要先跑探针打印实际值**（"先量基准再定阈值"的延伸）：
  实测踩到两条 —— P 模式 PNG 的 PLTE **固定带满 256 槽**（不是板的真实条目数，`#spr.palettes[1]`=256）；
  `ChangePixelFormat{format="rgba"}` **不认 `transparentColor`**（索引 0 按板条目 alpha=255 转成不透明黑）。
- **交付前必查透明**：`verify_artifact` 的 PASS **盖不住透明丢失**（alpha 全 255 也算"二值"）⇒
  任何 PNG 产物都要把 `opaque_px` 与源对齐核一遍。
- **失败脚本留痕（2026-10-15 新增）**：`scripts/aseprite-lua.mjs` 在 Lua 报错 / Aseprite 非零退出 /
  结果通道里没有 `__RESULT__` 三种情况下，会把**Aseprite 真正跑的那份脚本**、调用方的原始片段、
  结果通道、以及真实报错写到 **`<scratchDir>/_fail/fail-<时间戳>-<uuid>.{lua,body.lua,out.txt,note.txt}`**
  （`ASEPRITE_FAIL_DIR` 改目录；`ASEPRITE_KEEP_LUA=1` 连成功也留）。
  ⇒ **失败时先读 `_fail/*.note.txt`**（里面有原样报错），别再从行号反推调用链——
  `fail-*.lua` 里的行号就是报错行号所指的那份文件。成功不留痕（除 `ASEPRITE_KEEP_LUA=1`）。
- **量尺与优化器必须同度量**：用 CIE L\* 排序却用 `0.299/0.587/0.114` 判单调 ⇒ 误报非单调。
- **光向必须先从素材实测，禁止假设**（本轮 AI 假设 NW=315°，实测众数 **340°**，分线全偏）。
  工具 `vision/light-direction.py`（亮度一阶矩，9/9 自检）。**整幅场景的估计不可信**：
  底板测出 220°SW 是**反照率干扰**（底部水面/路面本来就亮）⇒ 只采信单品/多件众数。

## 光源模拟器与分层光影（2026-10-14 定稿，用户选 A）

- 规格单一真源：`aseprite/spec/pixel-spec.json`（画布/调色板/光源/分带/图层命名/8 条闸门）。
  已定：**key 340°/45° 单主光**，环境 0.22，分带 3 阶（阴影 q0.30/0.18/0.08，高光 q0.80），分线只画主线。
- `vision/light-rig.py`（自检 8/8）：多光源/仰角/权重模拟器。
  预设 `nw_key / measured / key_fill / top / backlight / candle(负仰角) / two_side`；
  自定义 `--rig "key:340,45,1.0;fill:160,35,0.35"`。
  输出：明暗场 · **主分界线（1px，滤 <12px 连通块）** · 分带上色 · **4 张分层 PNG** · 并排对照表。
- **选光源看 `unlit_share`（绝对未照到比例），不要看 `band_share`**：后者由分位阈值构造，每个光源都一样。
- 图层契约：`00_base`（固有色，压平到族内用量中位阶）· `01_shadow` · `02_highlight`（索引色+二值 alpha，可整层换/交引擎 ramp 换色）· `03_terminator_guide`（仅参考，不进引擎）。
- **双闸门**（12/12 通过）：①磁盘回读层数/层名/每层指纹 == 源 PNG；②三层复算合成 == 合成图 `changed=0`（`stack-layers.py` + `image-diff.py` 交叉验证）。
- **路线结论**：逐像素重映射（`relight-split.py`）**退役**（文件已移出 `vision/`，
  见 `aseprite/_retired/README.md`），改走分层绘制——固有色层与光源逐像素无关（同一固有色层在 7 组光源下完全相同），换光只换光影层。
- 写代码时的三个已实测陷阱：**分位阈值用 `<` 会得到空集**（大量像素被 clip 到环境值时 q(0.30)==最小值）⇒ 用 `<=`；
  **法线场必须先平滑距离场**（整数 DT 直接求梯度得到噪声法线）；
  **`mid==hi` ⇒ 高光层 0 像素**，必须夹紧 `lo < mid < hi`。

## 大图立绘（512×768 级）· 2026-10-16 farm02 实测（全在真 Aseprite 上验过）

来源：`aseprite/projects/farm02/portrait/README.md`（含脚本 `tools/pixelize-live.py`、`portrait/*.py|.lua`）。
四条能直接复用的结论：

- **"生图 → 像素立绘"必须整数倍降采样**：源图 1664×2496 → 目标 512×768 是 3.25×，
  非整数倍会让块边界落在源像素中间 ⇒ **线条粗细逐段变化**（这正是 mota3000 那批手工
  256×384 立绘"线条不能保持"的成因）。做法：先裁到 k 的整数倍，再**块众数**降采样（k=3 → 332×729）。
  ⚠ k 的搜索**必须取"输出能装进画布"的最小 k**；取最大 k 会得到 31×68 那种废图（判据单调，别写反）。
- **`--gpl` 的索引 0（透明槽占位 `(0,0,0)`）必须排除出量化**：否则近黑描边像素被量化到它上面
  （实测 **18727 px**），RGBA 里 `(0,0,0)` 与 `#171310` 肉眼几乎一样 ⇒ 看不出；
  **一进 INDEXED 就整片变透明**（opaque 122043→103316、剪影碎成 **54** 块）。
  反向也设一条闸门：索引化时**不把索引 0 放进 LUT**，让"不透明像素却是 (0,0,0)"**报错**而不是静默变透明。
- ⛔ **INDEXED 稿的透明有三条独立坑**（都会静默毁图）：
  ① `app.command.ChangePixelFormat{format="rgba"}` **不认 `transparentColor`** —— 它按调色板条目的
  alpha(=255) 把索引 0 转成**不透明黑**（实测 122043→393216）⇒ 透明必须**自己按源索引清成 `Color(0,0,0,0)`**；
  ② 把 INDEXED 精灵**导出成 PNG 写的是索引 PNG 且不带 tRNS 块** ⇒ 别的工具读它时索引 0 = 不透明黑
  （实测 `opaque_px=393216` = 整幅）⇒ 交付 PNG 走 RGBA，且顺序必须是**先存 `.aseprite`（保持索引稿）、
  再在内存里换格式导 PNG**；③ P 模式 PNG 用 PIL `.convert("RGBA")` 同样丢透明 ⇒ 手工按 `idx>0` 造 alpha。
  ⇒ **`verify_artifact` 的 PASS 盖不住这类错**（alpha 全 255 也算"二值"、板外色也是 0）：
  必须自己读 metrics 的 **`opaque_px`** 对不对得上。这是"PASS ≠ 全规格达标"的活教材。
- **左右对称化（把不对称的生图改成对称）**：在**索引图**上绕中轴镜像（镜像 = 索引搬运 ⇒ 不可能引入板外色）。
  两条铁律：
  ① **镜像轴必须从"本身对称的部位"实测**（草帽/头颈/肩的中轴，本次五条独立路径一致 = **218**），
  **不能用内容 bbox 中心**（本次 255.5，被右侧伸出的**洒水壶**拽偏 37px；绕它镜像会把围兜啃成两半、
  胸口露皮肤 —— 视觉上一眼可见，但闸门**抓不到**）；
  ② **只把一侧贴到另一侧**（`--source left|right`）。对生效集合全体写 `mirror[sel]` 是**两半互换**、
  不是对称化 —— 判据"**生效集合内镜像差 = 0**"能当场咬住（实测会报 18242）。
  另有两条设计：**只在"该像素与它的镜像都不透明"的位置生效**（⇒ 手臂/道具剪影完全不动，
  不会"镜像把剪影啃掉一块"）；闸门 C 只禁**新增**索引（镜像搬运会让被覆盖侧独有的颜色合法消失）。
- **P 模式 PNG 的 PLTE 固定带满 256 槽** ⇒ Aseprite 打开后 `#spr.palettes[1]` 是 256，
  **不是板的真实条目数**。顺带得到一条更好的闸门：**断言"没有像素用到索引 ≥ 板条目数"**，
  再 `pal:resize(N)` 到真实条目数。
- **`scripts/aseprite-lua.mjs` 是库不是 CLI**（头部只 `export`，无入口）⇒ `node <它> <x.lua>`
  只 import 一下、**退 0 且什么都不做**。要跑 Lua 文件用 **MCP `run_script` + `dofile("<路径>")`**。
- ⚠ **PS5.1 控制台是 GBK**：`.py` 里 `print` 中文全是乱码，且遇到 `⚠` 这类字符会直接
  `UnicodeEncodeError` 崩掉。所有脚本开头加
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。
- **立绘拼板法**（用户口径"素材偏暗但**立绘可以丰富点**"）：板 = **角色材质族**（身份一致）
  + **场景板的专属族**（做旧牛仔 `07_solarblue`）。并写成断言：**除指定族外，所有色相必须落在暖棕域**
  （否则就重犯"借专属色"那条已判定缺陷）。`.gpl` 载入时逐条比对**实物板**，规格表与实物不一致时报出来
  （实测：`CHAR-V2 §2` 表的 skin/hat 与 v3 实物板不符 —— v3 把这两族去饱和了）。

### 「画出来」≠「像素缩小」（2026-10-16，同一分支第二轮）

用户验收时说 **"你得画出来哦 / 而不是只是像素缩小"**。判据（先量基线，别凭感觉）：

| 指标 | 缩小版 | 画出线版 | 真像素画 |
|---|---|---|---|
| 描边占人物比 | 22.7% | **7.8%** | 6–10% |
| 描边段长中位 | 3–4 px | **1 px** | 1 |
| 2×2 全描边块 | 18293 | **24**（0.25%） | 0（斜向交叉结点除外） |
| 描边 8 邻连通块 | 37（断续） | **1** | 1 |
| 每族用阶 | 4–8 且**跳阶** | **5 单调** | 有限且单调 |

**三条判据我先写错了，都当场返工（记下来别再犯）**：
- ❌ "描边连通块 == 1"：**一幅画的内部线work 本来就是很多段独立笔画**（原稿实测 31 段）。
  该禁的是"**细化把线切碎**" ⇒ 判据 = `块数 ≤ 原描边块数`。
- ❌ "描边 2×2 == 0"：**两条斜线交叉的结点在 8 连通下 2×2 是拓扑必需的**
  （收尾的 `break_2x2` 一个都删不掉，正因为它们全都不是单点）。单条 Bresenham 斜线不会产生 2×2。
  ⇒ 主判据 = **`线宽中位 == 1`**，2×2 只卡结点量级（≤线像素 1%）。
- ❌ 缺一条"细节保留率"闸门 ⇒ **眼睛被掏空而所有闸门全绿**。补上：
  `指定框内 L*<28 像素保留 ≥ 80%`（实测第一次 57% 被判 FAIL，修好后 86%）。

**四条实现坑（全静默）**：
1. ⛔ 不能只对整块描边跑 Zhang-Suen：骨架落在描边**正中** ⇒ **外侧那一圈**像素的邻居只有骨架与透明区，
   没有材质可传播（当场 `INPAINT_NO_SEED`）。外轮廓必须**贴剪影边**（`op & ~erode`），
   只有内部线work 才取中轴骨架。
2. ⛔ 回填用**一次覆盖整个剪影的多源 BFS**（可穿过线像素，线像素随后覆写）；
   "逐轮找带材质的邻居"必然在孤立区卡死。
3. ⛔⛔ **深阴影必须有自己的落点**：实测皮肤 ramp 最暗只到 L49 而鼻影需 L≈25 ⇒ 量化器只能把深阴影
   归到**描边色**，于是线与阴影同色、细化一视同仁 ⇒ **脸塌平 + 眼睛被掏空**。
   修法 = 每族**向下补 2 阶深阶**（描边色 ↔ 该族最暗锚点之间插值）；
   ⚠ 补的阶**必须按 L 升序插**（板是 ramp 连续索引，写反过一次 ⇒ L41→L30→L49 非单调，取"最暗阶"取错）。
4. ⛔ 被细化掉的暗像素**不能走"最近材质值"**（暗区四周是亮皮肤 ⇒ 暗部被整体提亮，保留率仅 57%）。
   正解 = **线 + 暗阶**：暗区里除线以外的像素统一给该族**最暗一阶**。

**工程纪律**：族索引范围**不要硬编码**（本次硬编码 `NENTRY=50` 已踩一次）——
让板脚本吐一个 `.lua`/`.json` 的族表，下游 `dofile`/读 JSON；Lua 侧 `dofile` 一个返回表的文件最省事。

### 抠图：先全局阈值，再用 Aseprite 油漆桶补刀（2026-10-16）

**用户口径**："抠图可以用 aseprite 的魔法棒哦"。实测确实该这么分工：

- **全局颜色阈值**（到背景色的最大通道差 > 60）定角色骨架 —— 它快、可复算，且**不受背景渐晕影响**。
- **Aseprite 油漆桶**（`app.useTool{tool="paint_bucket", tolerance=T, contiguous=true}`，
  种子取四角+四边中点）负责**缝隙里的软阴影（AO）**：AO 比背景暗 30~40，
  正好卡在阈值边缘 ⇒ 阈值低了留一条灰 sliver（"手肘没抠干净"），高了把角色内部中间色也切掉
  （**实测阈值 80 就掉 15% 主体、100 直接腰斩** —— 因为角色内部很多中间色本来就靠近背景灰，
  抬阈值会断连通性）。油漆桶按容差**连续扩散**，AO 在容差内被吃掉、硬描边挡住它。
- ⛔ **别指望油漆桶单独搞定**：背景有**渐晕**，种子色容差覆盖不到全幅（实测只覆盖 73%）。
  ⛔ 也**别指望它涂洋红后的掩膜**：`(dist>阈值) 且 非洋红` 才是对的组合
  —— 油漆桶的渐晕漏网无所谓（那些像素本来就不在 `dist>阈值` 里）。
- ⛔ **Aseprite 存 PNG 会丢 alpha**（实测三种表现）：INDEXED 导出写的是索引 PNG 且无 tRNS；
  RGB 导出直接没有 alpha 通道；`ChangePixelFormat{format="rgba"}` 不认 `transparentColor`。
  ⇒ **走洋红键控**（项目既有约定"纯色可抠底 + 确定性去底"），Python 按洋红抠，
  完全不依赖 PNG 的 alpha。另：`app.useTool` 是**会"成功但什么都没做"**的 ⇒
  必须自检种子点真的被涂了（本次四角检查 PASS 才发现"没生效"其实是存盘丢 alpha 造成的误判）。

### 两个"边缘幽灵"根因（都会静默，都靠闸门/度量抓出来）

1. **中性暗像素被"金属族补的深阶"吸走**：源插画的线work 是**纯黑（中性）**，它的抗锯齿是**中性暗灰**；
   而为了让深阴影有落点，我给每族向下补了 2 阶 —— 金属族补出来的 `#474440/#35322f` **也是中性**
   ⇒ 成了中性暗像素的磁铁，把每条轮廓的抗锯齿都吸成"金属灰"，沿手臂/胸口留下幽灵金属
   （实测手肘 79px 一个连通块、胸口 806px 一块）。
   修法 = **「中性 + 暗」一起判**（`L* < 45 且 sat < 0.12` → 强制归描边族）：
   只用亮度判会连**皮革的暖色暗阶**（sat≈0.5）一起吃掉。
2. **块众数投票把背景像素也算进去**：一个"九宫格大部分是背景"的块，
   alpha 靠角色像素过 50% 变不透明，**颜色却被背景灰（中性、L≈59）赢走** ⇒ 量化到金属族亮阶。
   修法 = `block_mode` 只让**角色掩膜内**的像素投票。

**仍残留（未解决，如实留档）**：手肘那条带**还剩下约 80px 金属灰**（改前 74px；胸口那 806px 集群已消除）。
已知的剩余候选机制：redraw 里"暗像素 → **最近材质族**的最暗阶"可能选中金属族（当邻近材质被量化成中性灰时）。
⇒ 候选修法：把金属族从 redraw 的 `fam_lo` 赋值里排除；或写成空间规则"金属族只在洒水壶/腿布附近合法"。

### 跑长链路：落成 `.ps1`（并注意执行策略）

链路超过 3 步就该落成一个脚本（本次 5 步、重复跑了 4 次以上）。两条实测：
- ⛔ **本机禁止直接执行 `.ps1`**（`UnauthorizedAccess` / PSSecurityException）⇒
  用 `powershell -NoProfile -ExecutionPolicy Bypass -File <路径>`。
- ⛔ `.ps1` 里**只用 ASCII**：PS 5.1 按 ANSI/GBK 读脚本，中文注释会乱码。
  另外脚本里的默认参数会**覆盖** Python 侧的默认值（本次 `-DarkL 14` 把 Python 的 45 盖掉，
  白跑一轮）⇒ 改默认值要**两边一起改**。
