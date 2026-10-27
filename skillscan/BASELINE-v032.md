# 基线对比：v0.3.0（旧） vs v0.3.2（新）

> 生成：2026-10-27 · 数据文件：`data/results.jsonl`（旧基线 330 仓）、`data/results_v032.jsonl`（新基线 432 仓）
> 规则：v0.3.0 → v0.3.2 的两处修复见 `REVIEW-003.md` 与 rules.json changelog
> 所有数字可由本仓库内脚本复算：见文末复算命令

## TL;DR

1. **432 个仓库用 v0.3.2 全量重扫**（ruleset v0.3.2），分布 A 249 / B 138 / C 34 / D 11。
2. 与旧基线可比的 317 仓中，**232 个（73.2%）降分**（误报修正，平均 -9 分）；**28 个升分**（低估修正，平均 +23 分）；57 个不变。
3. **13 个仓库从"看似安全"（<40）直接暴露为真风险（≥60）**——包括 ChromeDevTools/chrome-devtools-mcp（Chrome 官方团队）。这正是"文档命中顶掉运行时命中"bug 的直接受害者名单。
4. 误报修正的最大案例：**microsoft/SkillOpt 100 → 27**（旧扫描器把微软官方仓库误杀到满分危险）。
5. 危险区（≥60）仓数：16 → 19。看似只多 3 个，实际是"16 个误报仓库离开危险区 + 13 个真风险仓库进入"。

## 分布对比

| Grade | v0.3.0（330 仓） | v0.3.2（432 仓） |
|---|---|---|
| A | 128 (38.8%) | 249 (57.6%) |
| B | 146 (44.2%) | 138 (31.9%) |
| C | 47 (14.2%) | 34 (7.9%) |
| D | 11 (3.3%) | 11 (2.5%) |

解读：A 类占比 38.8% → 57.6%，主要是语境降级体系（文档/清单/CI 命中按 low 权重计）发挥了预期作用；D 类 11 → 11 但成员大幅换血（旧 D 仓重扫后 5 个保住 D，4 个降到 B，1 个降到 C），新进 D 仓多为真中危。

## 低估修正名单（28 个升分仓的关键子集）

"看似安全 → 真危险"（旧分 <40 且新分 ≥60）：

| 仓库 | 旧分 | 新分 |
|---|---|---|
| mksglu/context-mode | 38 | 82 |
| hkcanan/katmer-code | 38 | 71 |
| coleam00/helpline | 32 | 65 |
| level09/enferno | 20 | 64 |
| eatmoreduck/boss-zhipin-scraper | 35 | 64 |
| alchaincyf/huashu-design | 28 | 61 |
| hashgraph-online/skill-publish | 13 | 61 |
| ChromeDevTools/chrome-devtools-mcp | 17 | 46 |

完整 28 仓名单可用文末复算命令提取。（ChromeDevTools/chrome-devtools-mcp 新分 46 未到 60，但因暴露出运行时中危命中，一并列入名单说明。）

## 误报修正的最大案例（旧分 ≥90）

| 仓库 | 旧分 | 新分 |
|---|---|---|
| microsoft/SkillOpt | 100 | 27 |
| SnailSploit/Claude-Red | 100 | 39 |
| zenbu-labs/terminal-browser | 86 | 37 |
| onyx-dot-app/onyx | 88 | 39 |
| BigPizzaV3/CodexPlusPlus | 68 | 12 |
| addyosmani/agent-skills | 66 | 21 |
| HKUDS/CLI-Anything | 57 | 12 |

## 口径说明

- 旧基线 332 行 = 330 唯一仓库（2 个重复扫描）；新基线 432 仓（452 目标中 20 个因 API 抓取失败未完成，不影响结论）。
- 交集 317 仓参与对比；新增 115 仓仅有新基线分数。
- "低估修正"= 修复了 `score_of()` 同规则只取第一条命中的 bug（文档命中顶掉运行时命中）；"误报修正"= 语境降级体系 `path_context()` 生效。

## 复算命令

```python
import json, statistics
old = {json.loads(l)['repo']: json.loads(l) for l in open('data/results.jsonl', encoding='utf-8')}
new = {json.loads(l)['repo']: json.loads(l) for l in open('data/results_v032.jsonl', encoding='utf-8')}
common = set(old) & set(new)
ups = [(old[r]['score'], new[r]['score'], r) for r in common if new[r]['score'] > old[r]['score']]
```
