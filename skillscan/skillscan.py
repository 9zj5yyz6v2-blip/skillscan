#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skillscan — Agent Skills / MCP Server 静态风险扫描器（种子版 v0.1）

设计原则（比功能更重要）：
  1. 纯静态：绝不执行被扫描的 skill 代码，绝不联网。
  2. 误报优先：宁可漏，不可乱报。规则总数刻意压在 30 条以内。
  3. 可解释：每条命中都给出「为什么」和「怎么改」，而不只是打分。

用法：
    python skillscan.py <目录或文件>
    python skillscan.py <目录> --json out.json
    python skillscan.py <目录> --fail-on C      # 评级 >= C 时退出码 1，用于 CI
    python skillscan.py <目录> --quiet          # 只输出一行结论

退出码：
    0 未触发阈值 / 1 触发阈值 / 2 参数或读取错误
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "rules.json")

TEXT_EXT = {".md", ".markdown", ".txt", ".py", ".sh", ".bash", ".js", ".mjs",
            ".cjs", ".ts", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next"}
MAX_FILE_BYTES = 512 * 1024
MAX_HITS_PER_RULE = 3

GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

COLOR = {
    "A": "\033[92m", "B": "\033[96m", "C": "\033[93m", "D": "\033[91m",
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def load_rules():
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_files(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in TEXT_EXT:
                yield os.path.join(dirpath, name)


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# 上下文降权：.md 里的「命令类」写法绝大多数是文档示例，不是要被执行的代码。
# 真正的执行风险在 .py/.sh/.js 里；.md 的原生风险是隐藏指令与提示注入。
DOC_EXTS = {".md", ".markdown", ".txt"}
DOC_NATIVE_CATS = {"hidden-instructions", "prompt-injection"}
DOWNGRADE = {"critical": "medium", "high": "low", "medium": "low", "low": "low"}

# ── 语境感知 ────────────────────────────────────────────────────────────
# 这是本项目最重要的一层。静态匹配无法区分「做某事」与「禁止做某事」，
# 而文档里大量句子是后者。把它们当成前者，会把安全建议判成恶意行为。
#
# 真实案例（本机 137 个 skill 实测）：
#   "Never silently downgrade to localStorage"   ← 禁止静默降级（安全约束）
#   "Do NOT silently deploy"                     ← 禁止静默部署（安全约束）
#   "never tell the user where to create one"    ← 禁止引导用户（防误导）
# 以上全部被旧规则判为 critical「要求隐瞒/跳过确认」，属于语义反转。

# 紧邻否定修饰：否定词必须紧贴匹配片段之前，才算修饰它。
# 用「紧邻」而不是「同句出现」，是为了避免 "we do not log this, but it uploads" 这类误降。
NEG_ADJ = re.compile(
    r"(?:\bnever|\bnot\b|n't|\bcannot\b|\bcan't\b|\bavoid\b|\bnever\s+ever)\s*$", re.I)

# 警告语境：这一行在讨论风险本身，而不是在实施风险
WARN_CTX = re.compile(
    r"(?:\bwarn(?:ing|s|ed)?\b|\brisk(?:s|y)?\b|\bcaution\b|\bdanger(?:ous)?\b|"
    r"\bpermanent\b|\birreversibl[ey]\b|\bdata[- ]wip\w*\b|\bloss\b|\bcareful\b|"
    r"警告|风险|注意|危险|不可逆|永久|误删|谨慎)", re.I)

# 代码注释行：描述意图，不是执行
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|--|<!--)")

# 良性隐瞒对象：隐瞒的是「实现细节」而非「风险事实」，属于正常的交互设计。
# 例："never mention same-name handling to the user"（不用内部同名逻辑打扰用户）
BENIGN_HIDE = re.compile(
    r"(?:same[- ]name|path|route|routing|internal|implementation|detail|"
    r"rule|flow|step|section|status|schema|format|语法|实现|路径|规则|流程|步骤|细节|内部)", re.I)


def resolve_severity(rule, cat, ext, line, before="", matched=""):
    """返回 (实际严重度, 语境标记)；严重度为 None 表示按上下文忽略。"""
    sev = rule["severity"]
    stripped = line.lstrip()

    # 1) 引用块（"> ..."）几乎总是转述第三方说法或文档片段，不构成行为
    if stripped.startswith(">") and cat["id"] not in DOC_NATIVE_CATS:
        return None, "quote"

    # 2) 语义反转：匹配片段被否定词紧贴修饰 → 这是「禁止做某事」，不是「要做某事」
    if NEG_ADJ.search(before):
        return "low", "negated"

    # 3) 代码注释 / docstring：描述的是意图，不产生执行行为
    if ext not in DOC_EXTS and COMMENT_LINE.match(line):
        return "low", "comment"

    # 4) 隐瞒的是实现细节而非风险事实 → 正常交互设计，不是恶意隐瞒
    if rule["id"].startswith("PI-0") and BENIGN_HIDE.search(line):
        return "low", "benign-hide"

    # 5) 警告语境：在讲风险的地方命中风险规则，属自指
    if WARN_CTX.search(line) and cat["id"] != "hidden-instructions":
        return DOWNGRADE.get(sev, sev), "warning-context"

    # 6) 文档文件里的命令类命中降两级：它描述的是"可能怎么用"，不是"我做了什么"
    if ext in DOC_EXTS and cat["id"] not in DOC_NATIVE_CATS:
        return DOWNGRADE.get(sev, sev), "doc-context"

    return sev, ""


def scan_file(path, text, categories, rel_root):
    hits = []
    rel = os.path.relpath(path, rel_root)
    ext = os.path.splitext(path)[1].lower()
    lines = text.splitlines()
    for cat in categories:
        for rule in cat["rules"]:
            try:
                rx = re.compile(rule["pattern"], re.IGNORECASE | re.MULTILINE)
            except re.error:
                continue
            found = 0
            for m in rx.finditer(text):
                if found >= MAX_HITS_PER_RULE:
                    break
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_no = text.count("\n", 0, line_start) + 1
                full = lines[line_no - 1] if line_no - 1 < len(lines) else ""
                line = full.strip()
                before = full[:max(0, m.start() - line_start)]
                sev, ctx = resolve_severity(rule, cat, ext, line, before, m.group(0))
                if sev is None:
                    continue
                hits.append({
                    "rule": rule["id"],
                    "severity": sev,
                    "severity_declared": rule["severity"],
                    "context": ctx,
                    "category": cat["id"],
                    "category_name": cat["name"],
                    "message": rule["message"],
                    "advice": rule["advice"],
                    "citation": rule.get("citation", {}).get("refs", []),
                    "file": rel,
                    "line": line_no,
                    "snippet": (line[:160] + "…") if len(line) > 160 else line,
                })
                found += 1
    return hits


def grade_of(score, grades):
    for g in grades:
        if score <= g["max"]:
            return g["grade"], g["label"]
    return "D", "不建议安装"


def severity_counts(hits):
    out = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for h in hits:
        out[h["severity"]] = out.get(h["severity"], 0) + 1
    return out


def dedupe(hits):
    """同一规则在同一文件只保留首条，避免单文件刷屏导致分数失真。"""
    seen, out = set(), []
    for h in hits:
        key = (h["rule"], h["file"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def score_of(counts, weights, hits):
    """每个规则只计一次（去重后），避免一处可疑写法被多条规则放大。"""
    rule_seen = set()
    s = 0
    for h in hits:
        if h["rule"] in rule_seen:
            continue
        rule_seen.add(h["rule"])
        s += weights.get(h["severity"], 3)
    return s


def scan_path(path, rules=None):
    """可复用入口：批量跑分、台账、报告都走这个函数，保证结果与 CLI 完全一致。"""
    rules = rules or load_rules()
    categories = rules["categories"]
    weights = rules["weights"]
    grades = rules["grades"]

    hits, file_count = [], 0
    for p in iter_files(path):
        text = read_text(p)
        if text is None:
            continue
        file_count += 1
        hits.extend(scan_file(p, text, categories,
                             path if os.path.isdir(path) else os.path.dirname(path)))

    hits = dedupe(hits)
    counts = severity_counts(hits)
    raw_score = score_of(counts, weights, hits)
    score = min(raw_score, 100)          # 对外统一到 0–100，避免单文件刷分失真
    grade, label = grade_of(score, grades)

    return {
        "target": os.path.abspath(path),
        "files_scanned": file_count,
        "score": score,
        "raw_score": raw_score,
        "grade": grade,
        "grade_label": label,
        "ruleset_version": rules["version"],
        "counts": counts,
        "findings": hits,
    }


def main():
    ap = argparse.ArgumentParser(description="Agent Skills / MCP Server 静态风险扫描器")
    ap.add_argument("path", help="要扫描的目录或文件")
    ap.add_argument("--json", dest="json_out", help="把完整结果写入 JSON 文件")
    ap.add_argument("--fail-on", dest="fail_on", choices=["A", "B", "C", "D"],
                    help="评级达到该等级及以上时，退出码为 1")
    ap.add_argument("--quiet", action="store_true", help="只输出一行结论")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        print(f"error: 路径不存在: {args.path}", file=sys.stderr)
        return 2

    result = scan_path(args.path)
    rules = load_rules()
    file_count = result["files_scanned"]
    hits = result["findings"]
    counts = result["counts"]
    score = result["score"]
    grade = result["grade"]
    label = result["grade_label"]

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    c = COLOR.get(grade, "") if sys.stdout.isatty() else ""
    r = RESET if sys.stdout.isatty() else ""
    b = BOLD if sys.stdout.isatty() else ""
    d = DIM if sys.stdout.isatty() else ""

    if args.quiet:
        print(f"{grade} {score:>3}  {os.path.basename(os.path.abspath(args.path))} "
              f"(critical {counts['critical']}, high {counts['high']})")
    else:
        print()
        print(f"{b}skillscan{r}  {d}ruleset v{rules['version']}{RESET if sys.stdout.isatty() else ''}")
        print(f"  目标  {result['target']}")
        print(f"  文件  {file_count}")
        print(f"  评分  {c}{b}{grade}{r}  {score} 分 · {label}")
        print(f"  命中  critical {counts['critical']} · high {counts['high']} · medium {counts['medium']}")
        print()

        if not hits:
            print("  未发现命中。注意：A 级的意思是「在本文规则集范围内没发现问题」，")
            print("  不等于安全 —— 静态规则永远有盲区。")
        else:
            order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            hits_sorted = sorted(hits, key=lambda x: (order.get(x["severity"], 9), x["file"]))
            cur = None
            for h in hits_sorted:
                if h["category"] != cur:
                    cur = h["category"]
                    print(f"  {b}{h['category_name']}{r}")
                tag = {"critical": "CRIT", "high": "HIGH", "medium": "MED ", "low": "LOW "}.get(h["severity"], "----")
                print(f"    [{tag}] {h['rule']}  {h['file']}:{h['line']}")
                print(f"           {h['message']}")
                if h["snippet"]:
                    print(f"           {d}› {h['snippet']}{r}")
                print(f"           {d}建议：{h['advice']}{r}")
                if h.get("citation"):
                    print(f"           {d}依据：{', '.join(h['citation'])}{r}")
                print()

        print(f"  {d}说明：纯静态扫描，不执行任何被扫描代码，不联网。规则集刻意精简，误报优先于覆盖率。{r}")
        print()

    if args.fail_on and GRADE_RANK.get(grade, 0) >= GRADE_RANK[args.fail_on]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
