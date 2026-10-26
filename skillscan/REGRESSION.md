# 误报回归测试报告

> 这是本项目最重要的一份文档。它记录的不是"我们抓到了什么"，
> 而是"**我们错判了什么，以及为什么错**"。

规则集：v0.2.0 → v0.3.0　样本：本机 137 个真实 skill　日期：2026-10-26

---

## 一、为什么要做这件事

安全扫描类产品只有两个指标：覆盖率、误报率。

**覆盖率决定它有用，误报率决定它能不能活。**

原因很直接：一个开发者被误报坑过一次，就再也不会用第二次；而且他会在 GitHub 上说出来。
一条误报的传播成本，远高于一百条正确命中的收益。

所以本项目的纪律是：**宁可漏报，不可误报。**

漏报的代价是"没发现"——而静态分析本来就有盲区，我们在每份报告里都写明了。
误报的代价是"被冤枉"——而冤枉的对象往往是要被我们公开点名的开源作者。

---

## 二、测试方法

```bash
# 1. 采集真实样本（本机已安装的 skill，按插件+版本去重）
python scan_local.py

# 2. 人工逐条复核所有 C / D 级样本，阅读原始文件上下文
#    判断标准：这是"文档里描述做法"还是"代码里真的执行"？
#             这是"禁止某行为"还是"要求某行为"？

# 3. 归因 → 改规则 → 重跑 → 对比
```

样本构成：137 个唯一 skill（239 个 SKILL.md，按插件与版本去重），
来自 WorkBuddy 内置技能库、插件市场缓存与用户级 skill 目录。
**这些是真实装在本机、真实在运行的 skill，不是网上随手找的仓库。**

---

## 三、结果

| 规则集 | A | B | C | D | 需人工复核 | 复核后确认为误报 |
|---|---|---|---|---|---|---|
| v0.2.0 | 91 | 35 | 9 | 2 | **11** | **9**（82%） |
| v0.3.0 | 112 | 23 | 2 | 0 | **2** | **0**（0%） |

同时，恶意样本的检出能力**未下降**：

| 样本 | v0.2.0 | v0.3.0 |
|---|---|---|
| evil-skill（人工构造的恶意 skill） | D 100 | **D 100** |
| good-skill（人工构造的正常 skill） | A 0 | A 0 |
| aihot（真实第三方 skill） | A 0 | A 0 |

> 关键：**降误报不等于降检出**。这是两件事，必须分别验证。

---

## 四、11 条误报的逐条归因

| # | Skill | 原判 | 命中原文 | 为什么错 | 归类 |
|---|---|---|---|---|---|
| 1 | library | critical | `No conflict → never mention same-name handling to the user.` | 隐瞒的是**内部同名处理逻辑**，为了不困扰用户，不是隐瞒风险 | 良性隐瞒 |
| 2 | library | high | `If the user names no destination, create it in the library…` | 正常的路由说明，不含隐瞒语义 | 上下文误判 |
| 3 | sites | critical | `never offer "create a new app" as a choice and never tell the user where to create one` | **"never tell" 是禁止**，语义完全反转 | 否定反转 |
| 4 | cloud-service | critical | `Never replace SDK module calls…, and never tell the user the SDK does not support miniprograms` | 同上，是安全约束而非隐瞒指令 | 否定反转 |
| 5 | cloud-service | high | `Never silently downgrade to localStorage` | **"Never silently" 是禁止静默**，被判成要求静默 | 否定反转 |
| 6 | sites | high | `Do NOT silently deploy.` | 同上 | 否定反转 |
| 7 | buddy-image-processing | critical | `response = requests.post(url, headers=headers, json=body)` | 调用**自己的后端 API**，任何联网 skill 都有这一行 | 规则过宽 |
| 8 | neodata-financial-search | critical | `resp = requests.post(url, headers=headers, json=payload)` | 同上 | 规则过宽 |
| 9 | library / tencent-docx | low→medium | `DROP TABLE / DROP FUNCTION — permanent loss` | 在**警告**风险的地方命中风险规则，属自指 | 警告语境 |
| 10 | tencent-docx | high | `Silently skips if indices are out of range.`（docstring） | 代码注释描述行为，不是执行行为 | 注释误判 |
| 11 | tencent-docx | high | `# Skip whitespace silently; warn on…` | 同上 | 注释误判 |

**9 条被确认为误报。剩下 2 条（见下节）是真命中。**

---

## 五、五类误报模式与对应检测

这五类不是"规则写得不够细"，而是**静态匹配无法区分语义方向**。每一类都需要专门的检测手段。

### 1. 否定反转（最严重）

> `Never silently downgrade` —— 这是**禁止**静默降级，旧规则判成"要求静默"。
> `never tell the user` —— 这是**禁止**告知，旧规则判成"要求隐瞒"。

**检测**：否定词必须紧贴匹配片段之前才算修饰它。

```python
NEG_ADJ = re.compile(r"(?:\bnever|\bnot\b|n't|\bcannot\b|\bavoid\b)\s*$", re.I)
# before 是匹配起点之前、同一行内的文本
if NEG_ADJ.search(before):
    return "low", "negated"
```

用「紧邻」而不是「同句出现」，是为了避免 `we do not log this, but it uploads` 这类被误降。
**这一条改动消除了 4 处 critical 级误报。**

### 2. 警告语境自指

> `DROP TABLE — permanent loss`（在警告你这么做会丢数据，结果被判为"要做破坏性操作"）

**检测**：行内出现 warn / risk / permanent / irreversible / 不可逆 / 风险 / 误删 → 降级。

### 3. 代码注释与 docstring

> `Silently skips if indices are out of range.` —— 这是 docstring 在描述函数行为

**检测**：`.py/.js/.sh` 中以 `#`、`//`、`/*`、`*` 开头的行 → 降为 low。
（不忽略，因为恶意说明本身也是有价值的信号，只是不该计分。）

### 4. 良性隐瞒

> `never mention same-name handling to the user` —— 不拿内部逻辑打扰用户

**检测**：隐瞒对象是实现细节类词汇（same-name / path / route / internal / rule / flow / 路径 / 规则 / 流程 / 细节）→ 降为 low。
**边界**：若隐瞒对象是「上传、删除、修改、读取凭证」这类风险事实，仍然是高危。

### 5. 规则过宽（唯一需要改规则本身的一类）

> `requests.post(url, json=body)` —— 任何调用自己后端的 skill 都有这一行

**原规则把 critical 判给了全世界。** 这是最典型的"把普遍现象当异常"。

**处理**：拆成两条。

- `EX-05`（low）：只要存在 POST/PUT，仅记录，不计分
- `EX-06`（critical）：载荷里出现 token / password / secret / credential / .env / id_rsa
  **或**目标域名是 webhook.site / requestbin / ngrok / pastebin / transfer.sh 这类一次性收料服务

> 判据从「有没有往外发」改成「**发了什么、发给谁**」。这才是真正有信息量的判断。

---

## 六、剩下 2 个真命中（这才是产品的价值证明）

### ① `tencent-docx` — C 58

```bash
# scripts/wb/local/setup-html-to-docx.sh:67
if ! curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1; then
```

真实的 `curl | sh`。来源是官方源（astral.sh），**大概率无害，但这个模式本身就该被记录**——
因为它意味着「安装这一步的执行内容，不受代码仓库的内容约束」。今天指向 astral.sh，明天上游被劫持就指向别处。

**这不是误报。正确的呈现方式是：记录它，标注"来源可信但模式有风险"，让使用者知情。**

### ② `workbuddy-migration-assistant` — C 44（10 critical / 3 high / 14 medium）

这个 skill 的功能就是**跨机器迁移你的全部 AI 资产**，所以它必然：

- 读 `.credentials.json`（OAuth refresh_token）
- 写 `mcp.json` / `settings.json`（PS-01）
- 执行 `DROP TABLE`（DE-02，清理目标表）

**这是正当功能，但也是真实的高权限行为。** 它属于最需要"知情同意"的一类：
用户应该明确知道"这个工具会碰我的凭证、会 drop 表"，而不是安装后才发现。

> **结论：连官方内置的迁移助手都需要 10 条 critical 级知情项。**
> 这就是这个产品存在的理由——不是因为有坏人在写恶意 skill，
> 而是因为**即使全是好人，也需要有人把"这东西会碰什么"写清楚**。

---

## 七、已知局限（必须对客户讲清楚）

1. **静态分析不执行代码**，因此无法发现运行时才拼装的恶意行为（如远程拉取二次载荷）
2. **无法验证依赖包本身**的供应链安全（只扫描 skill 自身内容）
3. **无法判断业务逻辑恶意**：一个正确调用了内部 API 的 skill，可能把数据发给了错误的对象
4. **语义反转只处理了紧邻否定**，跨行的否定（"不要做以下事情：\n1. 告诉用户…"）仍会误判
5. **prompt injection 类只能抓显式模式**，隐蔽的自然语言诱导无法覆盖

**每一条都写进客户报告。不掩饰盲区，是这个生意的信用基础。**

---

## 八、复现

```bash
cd skillscan

# 恶意/正常样本的检出能力
python skillscan.py fixtures/evil-skill --quiet   # 期望 D 100
python skillscan.py fixtures/good-skill --quiet   # 期望 A 0

# 真实样本分布
python scan_local.py                              # 输出 data/local_summary.md
```

任何人用同一份规则集扫描同一份代码，应得到完全相同的结果。
**可复现性本身就是产品的一部分**——否则报告没有证据效力。

## 第 4 轮 · 真实生态实测（332 仓库，2026-10-26）

批量扫描 GitHub 真实仓库 332 个（12.65M star）后，人工复核 7 个最惊人的 critical 发现：**7/7 全部误报**。

| 仓库 | 规则 | 误报原因 |
|---|---|---|
| microsoft/SkillOpt | CA-02 | 测试数据路径含 "KeyChain"（游戏道具） |
| onyx-dot-app/onyx | PI-01 | 防御性警告：教 AI 拒绝注入 |
| davila7/claude-code-templates | PI-01 | OWASP 教学示例 |
| addyosmani/agent-skills | PI-01 | 三条安全规则原文 |
| appwrite/appwrite | HI-01 | abuse 防护包的正常文案 |
| 1c7/chinese-independent-developer | EX-06 | 自有 token 调 GitHub API |
| elementalsouls/Claude-BugHunter | PI-01 | 漏洞赏金教学内容 |

**结论：** 静态规则无法消除的误报收敛为三类「意图问题」：讨论vs实施、数据vs行为、自用vs外发。
**产品决策：** C/D 级仓库在人工复核前不对外点名；报告以分布统计 + 误报研究为主。
**新增后置过滤：** 测试目录（test/data/fixture/example/workflows 等）命中不参与评级（results_filtered.json）。
