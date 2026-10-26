#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ledger.py —— 准入台账（这是整个生意的收费核心）

扫描器谁都能写，台账只有真的去做的那个人才有。
客户付钱买的不是"帮他扫一次"，而是：
  1. 一个完整的、不可篡改的记录：什么时候装了什么、谁批的、当时评几分
  2. 出事时两分钟内回答「我们有没有中招、影响哪个项目」
  3. 一份能交给审计/客户/投标的正式报告

设计原则：
  - 只追加，不修改：记录一旦写入不可变更（审计要求）
  - 每条记录可复现：记录 ruleset 版本与时间，任何人可用同版本复现同一结果
  - 决策留痕：谁批准的、什么时候、理由、有效期，全部留档

CLI:
  python ledger.py add <目录或仓库>   --name <名字> [--source <来源>]     # 扫描并入库
  python ledger.py list                                                   # 看所有记录
  python ledger.py diff                                                   # 和上一次比，有什么变化
  python ledger.py decide <名字> approve|reject --by <人> [--note <理由>]  # 记录人工决策
  python ledger.py alerts                                                 # 检查新出现的高危
  python ledger.py export [--out 文件名]                                   # 导出审计报告（Markdown）
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skillscan import scan_path, load_rules  # noqa: E402

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT.parent / "ledger" / "admission.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,               -- skill 的唯一名字
    source TEXT,                             -- 来源（目录 / 仓库 URL / 市场）
    first_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    decision_expires TEXT                    -- 批准有效期（可选）
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER NOT NULL REFERENCES skills(id),
    scanned_at TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,              -- 内容指纹：同一内容不重复计分
    grade TEXT NOT NULL,
    score INTEGER NOT NULL,
    counts_json TEXT NOT NULL,
    files_scanned INTEGER NOT NULL,
    target_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    context TEXT,
    category TEXT,
    file TEXT,
    line INTEGER,
    snippet TEXT
);

CREATE INDEX IF NOT EXISTS idx_scans_skill ON scans(skill_id);
CREATE INDEX IF NOT EXISTS idx_findings_scan ON findings(scan_id);
"""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def content_hash(path):
    """目录内容指纹：文件相对路径 + 内容的哈希。同一内容不重复计分。"""
    h = hashlib.sha256()
    root = Path(path)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for f in files[:2000]:                    # 上限保护
        rel = str(f.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        try:
            h.update(f.read_bytes()[:512 * 1024])
        except OSError:
            pass
    return h.hexdigest()[:16]


def cmd_add(args):
    con = connect()
    rules = load_rules()
    target = args.target

    if not Path(target).exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 2

    name = args.name or Path(target).name
    source = args.source or target
    cur = con.execute("SELECT id FROM skills WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        skill_id = row[0]
    else:
        cur = con.execute(
            "INSERT INTO skills(name, source, first_seen) VALUES (?,?,?)",
            (name, source, now()))
        skill_id = cur.lastrowid
        print(f"新登记: {name}")

    chash = content_hash(target)
    prev = con.execute(
        "SELECT id, grade, score FROM scans WHERE skill_id=? AND content_hash=? "
        "ORDER BY id DESC LIMIT 1", (skill_id, chash)).fetchone()
    if prev:
        print(f"内容未变（hash {chash[:8]}），复用上次结果：{prev[1]} {prev[2]} 分")
        return 0

    res = scan_path(target, rules)
    cur = con.execute(
        "INSERT INTO scans(skill_id, scanned_at, ruleset_version, content_hash, "
        "grade, score, counts_json, files_scanned, target_path) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (skill_id, now(), res["ruleset_version"], chash,
         res["grade"], res["score"], json.dumps(res["counts"], ensure_ascii=False),
         res["files_scanned"], str(Path(target).resolve())))
    scan_id = cur.lastrowid

    for h in res["findings"]:
        con.execute(
            "INSERT INTO findings(scan_id, rule, severity, context, category, "
            "file, line, snippet) VALUES (?,?,?,?,?,?,?,?)",
            (scan_id, h["rule"], h["severity"], h.get("context", ""),
             h.get("category_name", ""), h.get("file", ""),
             h.get("line", 0), (h.get("snippet") or "")[:200]))
    con.commit()

    c = res["counts"]
    print(f"评分: {res['grade']} {res['score']} 分  "
          f"(critical {c['critical']} / high {c['high']} / medium {c['medium']})  "
          f"{res['files_scanned']} 个文件  hash {chash[:8]}")
    return 0


def cmd_list(args):
    con = connect()
    rows = con.execute("""
        SELECT s.name, s.status, s.first_seen,
               sc.grade, sc.score, sc.scanned_at, sc.ruleset_version
        FROM skills s
        LEFT JOIN scans sc ON sc.id = (
            SELECT id FROM scans WHERE skill_id = s.id ORDER BY id DESC LIMIT 1)
        ORDER BY s.name
    """).fetchall()
    if not rows:
        print("台账为空。用 add 命令登记第一个 skill。")
        return 0
    print(f"{'名字':<34} {'状态':<9} {'评级':<4} {'分':>4}  {'最近扫描':<20} 规则集")
    print("-" * 100)
    for r in rows:
        print(f"{r[0]:<34} {r[1] or '-':<9} {r[3] or '-':<4} {r[4] if r[4] is not None else '-':>4}  "
              f"{(r[5] or '-')[:19]:<20} v{r[6] or '-'}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_diff(args):
    con = connect()
    rows = con.execute("""
        SELECT s.name, s1.grade, s1.score, s2.grade, s2.score
        FROM skills s
        JOIN scans s1 ON s1.id = (SELECT id FROM scans WHERE skill_id=s.id ORDER BY id DESC LIMIT 1)
        LEFT JOIN scans s2 ON s2.id = (
            SELECT id FROM scans WHERE skill_id=s.id AND id < s1.id ORDER BY id DESC LIMIT 1)
    """).fetchall()
    changed = [r for r in rows if r[2] and (r[1] != r[3] or r[2] != r[4])]
    if not changed:
        print("与上次相比没有变化。")
        return 0
    sev_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    print(f"{'名字':<34} {'上次':<8} {'这次':<8} 变化")
    print("-" * 80)
    for name, g1, sc1, g2, sc2 in changed:
        if g2 is None:
            arrow = "首次登记"
        else:
            worse = sev_order.get(g1, 9) > sev_order.get(g2, 9)
            arrow = "⚠ 恶化" if worse else ("✓ 改善" if sev_order.get(g1, 9) < sev_order.get(g2, 9) else "— 评分变了")
        print(f"{name:<34} {g2 or '-':<8} {g1:<8} {arrow}")
    return 0


def cmd_decide(args):
    con = connect()
    cur = con.execute("SELECT id FROM skills WHERE name=?", (args.name,))
    row = cur.fetchone()
    if not row:
        print(f"台账里没有这个名字: {args.name}", file=sys.stderr)
        return 2
    con.execute(
        "UPDATE skills SET status=?, decided_by=?, decided_at=?, decision_note=?, "
        "decision_expires=? WHERE id=?",
        (args.action, args.by, now(), args.note or "", args.expires or "", row[0]))
    con.commit()
    print(f"{args.name} → {args.action}（决策人: {args.by}）")
    return 0


def cmd_alerts(args):
    """检查：上次批准之后，有没有出现新的 critical/high。"""
    con = connect()
    rows = con.execute("""
        SELECT s.name, s.status, s.decided_at,
               (SELECT id FROM scans WHERE skill_id=s.id ORDER BY id DESC LIMIT 1) AS last_scan
        FROM skills s WHERE s.status = 'approved'
    """).fetchall()
    n = 0
    for name, status, decided_at, last_scan in rows:
        if not last_scan:
            continue
        scan_time = con.execute("SELECT scanned_at FROM scans WHERE id=?",
                                (last_scan,)).fetchone()[0]
        if decided_at and scan_time <= decided_at:
            continue
        cnt = con.execute(
            "SELECT COUNT(*) FROM findings WHERE scan_id=? AND severity IN ('critical','high')",
            (last_scan,)).fetchone()[0]
        if cnt:
            n += 1
            print(f"⚠ {name}: 批准之后出现了 {cnt} 个 critical/high（扫描时间 {scan_time[:19]}）")
    if n == 0:
        print("没有需要处理的告警。")
    else:
        print(f"\n共 {n} 个 skill 需要重新审查。")
    return 0


def cmd_export(args):
    con = connect()
    out = Path(args.out) if args.out else ROOT.parent / "ledger" / f"admission-report-{time.strftime('%Y%m%d')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    rules_v = load_rules()["version"]
    total = con.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    stats = con.execute("""
        SELECT s.status, COUNT(*) FROM skills s GROUP BY s.status
    """).fetchall()
    dist = con.execute("""
        SELECT sc.grade, COUNT(*) FROM skills s
        JOIN scans sc ON sc.id=(SELECT id FROM scans WHERE skill_id=s.id ORDER BY id DESC LIMIT 1)
        GROUP BY sc.grade
    """).fetchall()

    L = []
    a = L.append
    a("# Agent Skill 准入台账 · 审计报告")
    a("")
    a(f"> 导出时间：{now()}　规则集：v{rules_v}　登记总数：{total}")
    a("")
    a("## 决策状态")
    a("")
    a("| 状态 | 数量 |")
    a("|---|---|")
    for k, v in stats:
        a(f"| {k} | {v} |")
    a("")
    a("## 风险分布（最近一次扫描）")
    a("")
    a("| 评级 | 数量 |")
    a("|---|---|")
    for k, v in dist:
        a(f"| {k} | {v} |")
    a("")
    a("## 台账明细")
    a("")
    rows = con.execute("""
        SELECT s.name, s.source, s.status, s.decided_by, s.decided_at, s.decision_note,
               sc.grade, sc.score, sc.scanned_at, sc.ruleset_version, sc.content_hash
        FROM skills s
        LEFT JOIN scans sc ON sc.id=(SELECT id FROM scans WHERE skill_id=s.id ORDER BY id DESC LIMIT 1)
        ORDER BY s.name
    """).fetchall()
    for r in rows:
        a(f"### {r[0]}")
        a("")
        a(f"- 来源：`{r[1] or '-'}`")
        a(f"- 决策：**{r[2] or 'pending'}**" +
          (f"（{r[3]}，{r[4]}）" if r[3] else ""))
        if r[5]:
            a(f"- 决策说明：{r[5]}")
        a(f"- 最近扫描：{r[7] if r[7] is not None else '-'} 分（{r[6] or '-'} 级），"
          f"时间 {r[8] or '-'}，规则集 v{r[9] or '-'}，内容指纹 {r[10] or '-'}")
        a("")

    a("---")
    a("")
    a("本报告由 admission ledger 自动生成，所有记录可复现（含内容指纹与规则集版本）。")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"审计报告已导出: {out}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Agent Skill 准入台账")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="扫描并登记一个 skill")
    p.add_argument("target")
    p.add_argument("--name")
    p.add_argument("--source")

    sub.add_parser("list", help="查看所有记录")
    sub.add_parser("diff", help="与上次相比的变化")

    d = sub.add_parser("decide", help="记录人工决策")
    d.add_argument("name")
    d.add_argument("action", choices=["approve", "reject", "pending"])
    d.add_argument("--by", required=True, help="决策人")
    d.add_argument("--note", help="决策理由")
    d.add_argument("--expires", help="批准有效期（可选）")

    sub.add_parser("alerts", help="检查批准后新出现的高危")

    e = sub.add_parser("export", help="导出审计报告")
    e.add_argument("--out")

    args = ap.parse_args()
    fn = {"add": cmd_add, "list": cmd_list, "diff": cmd_diff,
          "decide": cmd_decide, "alerts": cmd_alerts, "export": cmd_export}[args.cmd]
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
