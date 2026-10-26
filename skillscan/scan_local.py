#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_local.py —— 扫描本机已安装的真实 skill（第一批真实样本）

为什么先用本机样本而不是 GitHub：
  1. 零网络依赖，不受 GitHub 速率限制影响
  2. 这些是「真实在运行的 skill」，不是陌生仓库，误报判断最可靠
  3. 可以直接告诉客户：你机器上装了多少个、有几个需要看

输出：
  data/local_results.jsonl   每个 skill 的完整结果
  data/local_summary.md      分布 + 需要人工复核的清单

用法：
  python scan_local.py
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skillscan import scan_path, load_rules  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "data"

SEARCH_ROOTS = [
    Path("C:/Users/Administrator/.workbuddy/plugins/cache"),
    Path("E:/workbuddy/resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills"),
    Path("C:/Users/Administrator/.workbuddy/skills"),
]

VER_RX = re.compile(r"/(\d+\.\d+(?:\.\d+)?)(?:[-.][^/]*)?/")


def ver_key(path_str):
    """从路径里抽出版本号，用于同名 skill 保留最新版本。"""
    m = VER_RX.search("/" + path_str.replace("\\", "/") + "/")
    if not m:
        return (0, 0, 0)
    parts = m.group(1).split(".")
    while len(parts) < 3:
        parts.append("0")
    return tuple(int(x) for x in parts[:3])


def find_skills():
    """返回 {唯一键: (skill 根目录, 显示名, 来源插件)}，同名保留最高版本。"""
    best = {}
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for md in root.rglob("SKILL.md"):
            d = md.parent
            # 唯一键：优先用「插件名::skill名」
            rel = d.relative_to(root)
            parts = [p for p in rel.parts if p not in ("skills", "skills_docs", "func-rule")]
            # 去掉版本号这类目录
            parts = [p for p in parts if not re.fullmatch(r"[\d.]+(?:[-.].*)?", p)]
            key = "::".join(parts) if parts else d.name
            vk = ver_key(str(d))
            if key not in best or vk > best[key][3]:
                best[key] = (d, d.name, root.name, vk)
    return {k: v[:3] for k, v in best.items()}


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    skills = find_skills()
    print(f"发现本机唯一 skill：{len(skills)} 个\n")

    rules = load_rules()
    out, rows = [], []

    for key, (path, name, source) in sorted(skills.items()):
        try:
            res = scan_path(str(path), rules)
        except Exception as e:
            print(f"  跳过 {key}: {e}")
            continue
        rec = {
            "key": key,
            "name": name,
            "source": source,
            "path": str(path),
            "score": res["score"],
            "grade": res["grade"],
            "counts": res["counts"],
            "files_scanned": res["files_scanned"],
            "findings": [
                {"rule": h["rule"], "severity": h["severity"], "context": h.get("context", ""),
                 "category": h["category_name"], "file": h["file"],
                 "line": h["line"], "snippet": h["snippet"], "message": h["message"]}
                for h in res["findings"]
            ],
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        out.append(rec)
        rows.append(rec)
        c = rec["counts"]
        mark = "  " if rec["grade"] in ("A", "B") else ">>"
        print(f"{mark} {rec['grade']} {rec['score']:>3}  "
              f"c{c['critical']}/h{c['high']}/m{c['medium']}  {key}")

    with (DATA / "local_results.jsonl").open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------- 统计 ----------
    n = len(out)
    dist = {}
    for r in out:
        dist.setdefault(r["grade"], []).append(r)
    sev = {k: sum(r["counts"][k] for r in out) for k in ("critical", "high", "medium", "low")}

    L = []
    add = L.append
    add("# 本机已安装 Skill 体检报告")
    add("")
    add(f"> 样本：{n} 个唯一 skill（已按插件与版本去重）　"
        f"规则集：v{rules['version']}　扫描时间：{time.strftime('%Y-%m-%d')}")
    add("")
    add("## 总览")
    add("")
    add("| 评级 | 含义 | 数量 | 占比 |")
    add("|---|---|---|---|")
    meaning = {"A": "未发现问题", "B": "低风险", "C": "需人工复核", "D": "不建议直接安装"}
    for g in ["A", "B", "C", "D"]:
        c = len(dist.get(g, []))
        add(f"| {g} | {meaning[g]} | {c} | {c/n*100:.1f}% |")
    add("")
    add(f"累计命中：critical {sev['critical']} · high {sev['high']} · "
        f"medium {sev['medium']} · low {sev['low']}")
    add("")

    risky = [r for r in out if r["grade"] in ("C", "D")]
    if risky:
        add(f"## 需要人工复核的 {len(risky)} 个")
        add("")
        for r in sorted(risky, key=lambda x: -x["score"]):
            add(f"### {r['grade']} {r['score']} · {r['key']}")
            add("")
            add(f"`{r['path']}`")
            add("")
            for f in r["findings"][:6]:
                add(f"- **[{f['severity']}]** {f['category']} · `{f['rule']}` "
                    f"— {f['file']}:{f['line']}")
                add(f"  - 片段：`{(f['snippet'] or '')[:140]}`")
            add("")
            add("> 判断要点：这是**文档里描述做法**（误报）还是**代码里真的执行**（真问题）？")
            add("")

    # 类别分布
    cat = {}
    for r in out:
        for f in r["findings"]:
            cat[f["category"]] = cat.get(f["category"], 0) + 1
    if cat:
        add("## 风险类别分布")
        add("")
        add("| 类别 | 命中次数 |")
        add("|---|---|")
        for k, v in sorted(cat.items(), key=lambda x: -x[1]):
            add(f"| {k} | {v} |")
        add("")

    add("## 边界说明")
    add("")
    add("- 纯静态扫描，不执行任何 skill 代码")
    add("- A 级 = 在规则集范围内未发现问题，**不等于安全**")
    add("- 本报告是内部体检，不构成对外认证")

    md = "\n".join(L)
    (DATA / "local_summary.md").write_text(md, encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"样本 {n} | A {len(dist.get('A',[]))} | B {len(dist.get('B',[]))} "
          f"| C {len(dist.get('C',[]))} | D {len(dist.get('D',[]))}")
    print(f"误报率基准（待人工标注）：需复核 {len(risky)} 个")
    print(f"明细 -> data/local_results.jsonl")
    print(f"报告 -> data/local_summary.md")


if __name__ == "__main__":
    main()
