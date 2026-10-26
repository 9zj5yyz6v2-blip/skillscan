#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py —— 客户评估报告生成器

生成一份可对外交付的正式报告（单文件 HTML，可直接打印/存档/发客户）。
与扫描器同一套规则、同一套语境逻辑，保证结果一致。

用法：
    python report.py <skill目录> --client "客户名" --no SCAN-20261026-001
    python report.py <skill目录>                       # 自动编号
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skillscan import scan_path, load_rules  # noqa: E402
from ledger import content_hash  # noqa: E402

ROOT = Path(__file__).resolve().parent

LIMITATIONS = [
    "本报告基于静态分析：只检查代码文本，不执行任何被检代码，因此无法发现仅在运行时才表现的恶意行为（例如运行中从远程拉取二次载荷）。",
    "本报告不验证第三方依赖包（pip / npm）自身的供应链安全，仅覆盖被检目录内的文件。",
    "本报告无法判断业务逻辑层面的恶意：一个调用内部 API 的行为可能完全合规，也可能把数据发给了错误的对象。",
    "语义反转检测仅处理紧邻否定（如 never tell / do not silently）；跨行否定（先总述禁止、再分条列举）仍可能产生误判。",
    "提示注入类规则只能覆盖显式模式，隐蔽的自然语言诱导无法被静态规则穷尽。",
    "评级 A 的含义是「在本报告规则集范围内未发现问题」，不等于「安全」。",
]

CTX_LABEL = {
    "negated": "否定语境（禁止性表述）",
    "warning-context": "警告语境（在讨论风险本身）",
    "comment": "代码注释/docstring",
    "benign-hide": "良性隐瞒（实现细节类）",
    "doc-context": "文档语境（描述用法）",
    "quote": "引用块",
}

SEV_COLOR = {"critical": "#dc2626", "high": "#ea580c", "medium": "#ca8a04", "low": "#64748b"}
GRADE_COLOR = {"A": "#16a34a", "B": "#ca8a04", "C": "#ea580c", "D": "#dc2626"}


def auto_no():
    return f"SCAN-{time.strftime('%Y%m%d')}-{time.strftime('%H%M%S')}"


def build_html(res, rules, client, report_no, target, chash):
    counts = res["counts"]
    grade = res["grade"]
    gcolor = GRADE_COLOR.get(grade, "#64748b")
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    hits = sorted(res["findings"], key=lambda x: (order.get(x["severity"], 9), x["file"]))

    # 语境降级的命中单独列出（透明呈现）
    downgraded = [h for h in hits if h.get("context")]

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    findings_html = ""
    if not hits:
        findings_html = ('<p class="muted">在规则集范围内未发现命中。</p>')
    else:
        cur = None
        for h in hits:
            if h["category"] != cur:
                cur = h["category"]
                findings_html += f'<h3 class="cat">{esc(cur)}</h3>'
            color = SEV_COLOR.get(h["severity"], "#64748b")
            refs = "、".join(h.get("citation") or []) or "—"
            ctx = ""
            if h.get("context"):
                ctx = (f'<span class="ctx" style="border-color:{color}">'
                       f'语境：{CTX_LABEL.get(h["context"], h["context"])} · 已降权</span>')
            findings_html += f'''
<div class="hit">
  <div class="hit-head">
    <span class="sev" style="background:{color}">{h["severity"].upper()}</span>
    <span class="rule">{esc(h["rule"])}</span>
    <span class="loc"><code>{esc(h["file"])}:{h["line"]}</code></span>
  </div>
  <div class="hit-msg">{esc(h["message"])}</div>
  <div class="hit-snippet"><code>{esc(h["snippet"])}</code></div>
  {ctx}
  <div class="hit-foot">依据：{esc(refs)}　·　建议：{esc(h["advice"])}</div>
</div>'''

    downgraded_html = ""
    if downgraded:
        rows = "".join(
            f"<tr><td><code>{esc(h['file'])}:{h['line']}</code></td>"
            f"<td>{esc(h['rule'])}</td><td>{CTX_LABEL.get(h['context'], h['context'])}</td></tr>"
            for h in downgraded[:20])
        downgraded_html = f'''
<h2>降权说明（透明度声明）</h2>
<p class="muted">以下命中因语境判断被降权或忽略。我们公开列出它们而不是隐藏，
因为「我们跳过了什么」和「我们发现了什么」同样重要。</p>
<table><thead><tr><th>位置</th><th>规则</th><th>降权原因</th></tr></thead>
<tbody>{rows}</tbody></table>'''

    lim_html = "".join(f"<li>{esc(x)}</li>" for x in LIMITATIONS)

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>{esc(report_no)} · Agent Skill 风险评估报告</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       color:#1f2430;background:#f6f7fa;font-size:15px;line-height:1.8;padding:40px 20px}}
  .page{{max-width:820px;margin:0 auto;background:#fff;border-radius:12px;
        box-shadow:0 2px 20px rgba(20,24,40,.08);padding:48px 52px}}
  .head{{display:flex;justify-content:space-between;align-items:flex-start;
        border-bottom:3px solid #6366f1;padding-bottom:20px;margin-bottom:26px}}
  .brand{{font-size:22px;font-weight:800;color:#6366f1}}
  .meta{{text-align:right;font-size:13px;color:#6b7280;line-height:1.9}}
  h1{{font-size:20px;margin-bottom:4px}}
  h2{{font-size:16px;margin:30px 0 12px;color:#111827;
     border-left:4px solid #6366f1;padding-left:10px}}
  h3.cat{{font-size:14px;margin:20px 0 8px;color:#4b5563}}
  .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}}
  .kpi{{background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;
       padding:14px;text-align:center}}
  .kpi b{{display:block;font-size:24px;font-weight:800}}
  .kpi span{{font-size:12px;color:#6b7280}}
  .grade{{font-size:46px;font-weight:800;line-height:1.1}}
  .muted{{color:#6b7280;font-size:13.5px}}
  .hit{{border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin-bottom:12px}}
  .hit-head{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
  .sev{{color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:5px}}
  .rule{{font-weight:700;font-size:14px}}
  .loc code{{font-size:12.5px;color:#6b7280}}
  .hit-msg{{font-size:13.5px;color:#374151;margin-top:6px}}
  .hit-snippet code{{display:block;background:#0f1117;color:#c9d1e6;font-size:12px;
      padding:8px 10px;border-radius:6px;margin-top:8px;white-space:pre-wrap;word-break:break-word}}
  .ctx{{display:inline-block;margin-top:8px;font-size:12px;color:#6b7280;
       border:1px dashed #d1d5db;border-radius:6px;padding:2px 8px}}
  .hit-foot{{font-size:12px;color:#6b7280;margin-top:8px;border-top:1px dashed #e5e7eb;padding-top:8px}}
  table{{width:100%;border-collapse:collapse;font-size:13.5px;margin:10px 0}}
  th{{text-align:left;background:#f3f4f6;padding:8px 10px;font-size:12px;color:#4b5563}}
  td{{padding:8px 10px;border-bottom:1px solid #f3f4f6;color:#374151}}
  code{{font-family:Consolas,monospace}}
  .sign{{margin-top:40px;padding-top:18px;border-top:1px solid #e5e7eb;
        display:flex;justify-content:space-between;font-size:13px;color:#6b7280}}
  .footer-note{{margin-top:26px;padding:14px 16px;background:#f9fafb;
       border-radius:10px;font-size:12.5px;color:#6b7280;line-height:1.9}}
  @media print {{ body{{padding:0;background:#fff}} .page{{box-shadow:none;padding:24px}} }}
</style></head><body><div class="page">

<div class="head">
  <div>
    <div class="brand">SkillScan · 准入评估</div>
    <h1>Agent Skill 风险评估报告</h1>
  </div>
  <div class="meta">
    报告编号：<b>{esc(report_no)}</b><br>
    出具日期：{time.strftime("%Y-%m-%d")}<br>
    委托方：{esc(client or "—")}<br>
    规则集：v{esc(res["ruleset_version"])}
  </div>
</div>

<table>
  <tr><th style="width:120px">被检对象</th><td><code>{esc(target)}</code></td></tr>
  <tr><th>内容指纹</th><td><code>{esc(chash)}</code>（同一内容可复现同一结果）</td></tr>
  <tr><th>检查文件数</th><td>{res["files_scanned"]}</td></tr>
  <tr><th>检查方式</th><td>纯静态分析：不执行代码、不联网、不修改任何文件</td></tr>
</table>

<h2>结论</h2>
<div class="grid">
  <div class="kpi"><b class="grade" style="color:{gcolor}">{grade}</b><span>综合评级</span></div>
  <div class="kpi"><b style="color:{gcolor}">{res["score"]}</b><span>风险分（0–100）</span></div>
  <div class="kpi"><b>{counts["critical"]}</b><span>critical 命中</span></div>
  <div class="kpi"><b>{counts["high"]}</b><span>high 命中</span></div>
</div>
<p class="muted">{esc(res["grade_label"])}。每条命中的判定依据均标注于对应条目内，可与公开标准（CWE / OWASP LLM Top 10 / MITRE ATLAS）逐条核对。</p>

<h2>命中详情</h2>
{findings_html}

{downgraded_html}

<h2>已知局限（务必阅读）</h2>
<ul style="margin:8px 0 0 18px;color:#374151;font-size:13.5px">{lim_html}</ul>

<div class="sign">
  <span>评估执行：SkillScan v{esc(res["ruleset_version"])}（自动化）+ 人工复核：＿＿＿＿＿＿</span>
  <span>复核日期：＿＿＿＿＿＿</span>
</div>

<div class="footer-note">
  本报告仅代表出具时点、针对上述内容指纹所对应版本的分析结论；被检对象后续更新不在本报告覆盖范围内。
  本报告可用于内部安全评审与交付存档，不构成对被检对象安全性的绝对担保。
  争议条款：对单条命中存疑的，可依据报告内标注的公开标准条目进行复核申诉。
</div>

</div></body></html>'''


def main():
    ap = argparse.ArgumentParser(description="生成客户评估报告")
    ap.add_argument("target")
    ap.add_argument("--client", help="委托方名称")
    ap.add_argument("--no", dest="report_no", help="报告编号")
    ap.add_argument("--out", help="输出路径（默认 reports/ 下自动命名）")
    args = ap.parse_args()

    if not Path(args.target).exists():
        print(f"路径不存在: {args.target}", file=sys.stderr)
        return 2

    rules = load_rules()
    res = scan_path(args.target, rules)
    chash = content_hash(args.target)
    report_no = args.report_no or auto_no()
    client = args.client or ""

    out = Path(args.out) if args.out else ROOT.parent / "reports" / f"{report_no}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(res, rules, client, report_no, str(Path(args.target).resolve()), chash),
                   encoding="utf-8")
    print(f"报告已生成: {out}")
    print(f"评级 {res['grade']} {res['score']} 分 · 编号 {report_no}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
