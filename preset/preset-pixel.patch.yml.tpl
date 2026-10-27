# 像素画（Aseprite）preset 声明 —— 由 install.ps1 / build-kit.mjs 填入工具包目录与 Aseprite 路径后追加到 profile 的 cordis.patch.yml。
# 本文件是模板：工具包目录与 Aseprite 路径在安装时由 install.ps1 填入（占位符不写在这里，免得被误替换）。
- insert:
    - id: preset-pixel
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: pixel
        name: 像素画（Aseprite）
        description: 像素画绘制与导出（Aseprite MCP），新建稿、先定画布、批量宏成图、细线像素完美
        order: 13
        plugins:
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: >-
                你是像素画师兼 Aseprite 操作员，用 Aseprite MCP（工具名 mcp__aseprite__*）把画面精确落到像素。
                铁律：① 只新建稿，绝不动用户既有美术资产（用户游戏项目的 asserts/ 等目录），要改先问；
                ② 动笔前先与用户定死画布尺寸/网格锚点/边界，尺寸不定不画；③ 外观（造型/配色/构图）由用户决定，
                你只负责精确实现，不替用户设计外观；④ 一张图用一段 Lua 批量画完（run_script），忌小步多轮反复微调；
                ⑤ 细线只用 draw_line 且 thickness=1（Bresenham 逐像素），禁用 thickness>1 的笔刷分支，粗线自己叠；
                ⑥ 画完导出 PNG 并出 ×8 放大预览，用 present 直接贴给用户——你不做审美验收，只做结构自检；
                ⑦ 写脚本/Lua/闸门前先读 skill 的「开工检查表」；脚本失败**先读 _fail/*.note.txt**（里面有原样报错），
                别再从行号反推调用链；⑧ 造型真源是 spec 里的 **ASCII 网格**——改图 = 改字母再重新出图
                （用 skill 的 scripts/cel-ascii.mjs，别每次手抄 dump/回写脚本）；⑨ 闸门**先量基准再定阈值**，
                每条判据配一个"能把它打挂"的负例，量尺 --selftest 全绿才准去验产物。
                开工前先加载 skill aseprite-pixel-art。
              suffix: Your working directory is the session workspace named in the current DSH file-policy line of your runtime context.
          - id: agent-instructions
            name: '@deepseek-ai/dsh-agent-instructions'
            config:
              maxBytes: 65536
          - id: tool-fs
            name: '@deepseek-ai/dsh-tool-fs'
          - id: tool-fs-search
            name: '@deepseek-ai/dsh-tool-fs-search'
            config:
              sampleOverCapGlobResults: false
          - id: tool-bash
            name: '@deepseek-ai/dsh-tool-bash'
            disabled: !!js process.platform === 'win32'
          - id: tool-pwsh
            name: '@deepseek-ai/dsh-tool-pwsh'
            disabled: !!js process.platform !== 'win32'
          - id: tool-jobs
            name: '@deepseek-ai/dsh-tool-jobs'
          - id: skill-filesystem
            name: '@deepseek-ai/dsh-skill-filesystem'
            config:
              customSkillDirs:
                - __PIXEL_TOOLKIT__\skills
          - id: tool-skill
            name: '@deepseek-ai/dsh-tool-skill'
          - id: tool-todo
            name: '@deepseek-ai/dsh-tool-todo'
            config:
              allowParallelInProgress: true
          - id: tool-ask-user
            name: '@deepseek-ai/dsh-tool-ask-user'
          - id: present
            name: '@deepseek-ai/dsh-tool-present'
          - id: mcp-aseprite
            name: '@deepseek-ai/dsh-mcp-client'
            config:
              serverName: aseprite
              command: node
              args:
                - __PIXEL_TOOLKIT__\mcp\build\index.js
              env:
                ASEPRITE_MCP_TMP: __PIXEL_TOOLKIT__\tmp
__ASEPRITE_PATH_LINE__
              transport: stdio
          - id: planning
            name: cordis:group
            group: true
            isolate:
              planMode: true
            config:
              - id: plan-mode
                name: '@deepseek-ai/dsh-plan-mode'
                config:
                  section: |
                    Plan mode exists to produce an approved plan, not to carry one out. Stay in plan mode until exit_plan_mode succeeds or the user leaves plan mode manually; agreement in conversation (including a direct yes to something you proposed) approves nothing by itself - fold it into the plan and submit the plan.

                    Ground the plan in the real workspace first: read files, search, and run read-only checks. While planning, do not write or edit files, change configuration, run formatters or code generation that rewrites tracked files, or commit.

                    Ask the user only about decisions that are theirs (art direction, scope, priorities) or about ambiguity that inspection cannot settle. Do not ask where code lives or how current behaviour works when you can find out.

                    A reviewable plan is decision-complete: the goal and acceptance criteria, the concrete changes grouped by area, the files and interfaces touched, ordering, edge cases and failure modes, and the assumptions you are making. Keep it tight - no code dumps.

                    Deliver the plan by calling exit_plan_mode with the plan as markdown starting with a '# ' title, as the only tool call in that response. Do not paste the plan as a plain reply and do not ask to proceed in prose. If the plan is rejected, revise and present again; if the review channel is unavailable, stay in plan mode and ask the user to switch modes.
          - id: compaction
            name: cordis:group
            group: true
            isolate:
              compaction: true
              toolResultPruner: true
            config:
              - id: compaction-basic
                name: '@deepseek-ai/dsh-compaction-basic'
              - id: command-compact
                name: '@deepseek-ai/dsh-command-compact'
              - id: tool-result-pruner
                name: '@deepseek-ai/dsh-compaction-tool-result-pruner'
                config:
                  thresholdChars: 8192
                  headChars: 4096
                  tailChars: 1024
