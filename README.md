# SkillScan — AI 技能（Agent Skills / MCP Server）静态风险扫描

> 在装上它之前，有人看过它里面写了什么。

**SkillScan** 是一个纯静态、零依赖的扫描器，用于检查 AI 技能包（Claude Agent Skills、MCP Server 等）中的敏感行为与风险模式。它**不执行任何被扫描的代码**，也**不联网**。

本文所属项目：[第 001 期生态体检报告](docs/) · [误报回归集](skillscan/REGRESSION.md) · [第 003 期 critical 全量复核](skillscan/REVIEW-003.md)

## 为什么需要它

你在 AI 里说一句"帮我装个读 Excel 的技能"，装上的可能是某个陌生人上周写的代码——它能读你的文件、能往任何网址发数据。手机装 App 有商店审核和权限弹窗，AI 装技能，什么都没有。

我们对 GitHub 上 **332 个真实仓库**（覆盖 1,265 万 star、6,262 个文件）做了首期体检：

| 评级 | 含义 | 数量 |
|---|---|---|
| A | 未见明显风险 | 128 |
| B | 存在一般性敏感操作 | 146 |
| C | 存在需人工确认的高危项 | 47 |
| D | 存在 critical 级命中 | 11 |

**重要结论：所有含 critical 命中的 48 个仓库已逐条人工复核完毕，0 个未披露的高危风险，62.5% 的 critical 仓库是误报；而且 50.6% 的 critical 命中根本不在运行时代码里**（在文档、插件清单、CI 脚本、测试夹具里）。静态扫描只能初筛——这恰恰是本项目公开误报全部过程的原因（见 [REGRESSION.md](skillscan/REGRESSION.md) 与 [REVIEW-003.md](skillscan/REVIEW-003.md)）。

## 快速开始

```bash
# 扫描一个 skill 目录
python skillscan/skillscan.py path/to/skill

# 只输出一行结论（用于 CI）
python skillscan/skillscan.py path/to/skill --quiet

# 评级达到 C 及以上时退出码为 1（可接入 GitHub Actions）
python skillscan/skillscan.py path/to/skill --fail-on C --quiet
```

要求：Python 3.8+，无任何第三方依赖。

## 8 类风险 · 31 条规则

每条规则均标注公开标准出处（CWE / OWASP LLM Top 10 (2025) / MITRE ATLAS）——"说你有问题"不是我们说了算，是标准说了算。

| 类别 | 示例 | 规则出处示例 |
|---|---|---|
| 隐藏指令 | 零宽字符夹带指令、要求对用户隐瞒 | MITRE ATLAS AML.T0051 |
| 凭证访问 | 读 `.env`、SSH 私钥、浏览器数据 | CWE-522 |
| 数据外发 | 敏感载荷发往一次性收料服务 | CWE-201 |
| 破坏性操作 | `rm -rf`、无备份覆盖 | CWE-78 |
| 提示注入 | 忽略既有指令、跳过用户确认 | OWASP LLM01 |
| 供应链 | `curl \| bash`、运行时远程拉码 | CWE-494 |
| 持久化 | 改写 shell 配置、注册定时任务 | CWE-250 |

完整定义见 [TAXONOMY.md](TAXONOMY.md)。

## 设计原则

1. **宁可漏报，不可误报。** 误报杀掉品牌，漏报只降低覆盖率（且已声明盲区）。五轮真实数据迭代，逐条复核到 critical 全覆盖，全过程公开。第 003 期发现：误报集中在四类「非运行时语境」——CI 脚本、测试夹具、插件清单元数据、教材里的攻击示例。
2. **文档 ≠ 行为。** `.md` 里的命令示例降权处理；"这是提示注入，别照做"和"这是提示注入，去照做"在正则眼里一样，所以我们不假装能区分——留给人工复核。
3. **点名前必人工复核。** 机器评级不构成对任何仓库的指控。

## 目录结构

```
skillscan/
  skillscan.py      # 扫描器（零依赖）
  rules.json        # 31 条规则 + 出处（v0.3.2）
  TAXONOMY.md       # 8 类风险分类法
  REGRESSION.md     # 误报回归集（5 轮迭代全过程）
  REVIEW-003.md     # 第 003 期：critical 命中全覆盖人工复核（含英文摘要）
  ledger.py         # 准入台账（SQLite）：登记/决策/告警/审计导出
  report.py         # 正式评估报告生成器
  harvest.py        # 生态测绘：仓库发现 + 批量扫描
  fixtures/         # 恶意/正常测试样本
data/
  results.jsonl     # 332 个仓库的首期扫描明细
  verdicts.json     # 64 条人工复核裁决（含原文依据）
  review_contexts_003.json  # 第 003 期复核的原文上下文（可逐条核对）
  rescan_v032.json  # 9 个仓库在 v0.3.0/v0.3.1/v0.3.2 下的同文件集对比
docs/               # 第 001/002 期报告（HTML）
```

## 局限（诚实版）

- 静态规则无法判断**意图**：讨论 vs 实施、数据 vs 行为、自用 vs 外发
- 不检测运行时行为、不执行代码、不做沙箱分析
- `.min.js`、二进制文件跳过

## License

MIT
