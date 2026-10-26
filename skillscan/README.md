# skillscan — Agent Skills / MCP Server 静态风险扫描器

种子版 v0.2.0。纯静态、零依赖、不执行被扫描代码、不联网。

**v0.2 变更**：29 条规则全部可归因到公开标准（CWE / OWASP LLM Top 10 2025 / MITRE ATLAS），报告中每条命中都会打印「依据」。争议时引用标准，不引用作者判断。

## 三条命令跑起来

```bash
# 1. 扫描一个 skill 目录
python skillscan.py ./my-skill

# 2. 只输出一行结论（适合批量跑榜单）
python skillscan.py ./my-skill --quiet

# 3. CI 模式：评级达到 C 及以上时退出码为 1，可用来拦截合并
python skillscan.py ./my-skill --fail-on C --quiet
```

其他参数：

| 参数 | 作用 |
|---|---|
| `--json out.json` | 输出完整结果（含每条命中的文件、行号、上下文） |
| `--quiet` | 只打印一行：等级 + 分数 + 统计 |
| `--fail-on A\|B\|C\|D` | 阈值退出码，用于 CI |

## 目录结构

```
skillscan/
├── skillscan.py        扫描器（约 200 行，纯标准库）
├── rules.json          28 条规则，8 个类别，改这个文件就能加规则
├── TAXONOMY.md         分类法全文，可直接对外发布
└── fixtures/
    ├── evil-skill/     恶意样本：隐藏指令 + 凭证读取 + 渗出 + 强删 + 定时任务
    └── good-skill/     正常样本：应判 A
```

## 设计约束（不要轻易违反）

1. **纯静态**：绝不执行被扫描代码。扫描器一旦会跑代码，它比它要防的东西更危险。
2. **误报优先**：规则总数压在 30 条以内。覆盖率是虚荣指标，误报率是生死指标。
3. **文档降权**：`.md` 里的命令类写法通常是在描述安装方式，不是执行行为，降两级；引用块（`> ...`）直接忽略。
4. **A 级 ≠ 安全**：A 只代表"在本规则集可见范围内没发现问题"。任何把 A 说成安全认证的行为都会毁掉标准本身。

## 已知盲区

动态行为、语义级拆分攻击、依赖包内部实现、多语种（中英以外）隐蔽表达。详见 `TAXONOMY.md` 末章。

## 回归测试

```bash
python skillscan.py fixtures/evil-skill --quiet   # 应为 D 100
python skillscan.py fixtures/good-skill --quiet   # 应为 A 0
```

改完 `rules.json` 后务必跑这两条，确认没有把恶意样本漏掉、也没有给正常样本误报。
