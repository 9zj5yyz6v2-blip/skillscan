"""复核辅助：为 C/D 级仓库的 critical/high 命中拉取原文上下文。
产出 data/review_contexts.json，供人工逐条裁决。
用法: python review_fetch.py [--min-stars 0]
"""
import json
import base64
import sys
import time
import harvest

SRC = harvest.DATA.parent / "data" / "results_filtered.json"
OUT = harvest.DATA.parent / "data" / "review_contexts.json"

NOISE = harvest_re = __import__("re").compile(
    r"(^|[/\\])(test|tests|testdata|data|fixture|fixtures|mock|mocks|example|examples|samples?|__tests__|node_modules|workflows|dist|build)([/\\]|$)", __import__("re").I)


def fetch_context(repo, path, line, ctx=4):
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref=HEAD"
    d = harvest._get_json(url)
    text = base64.b64decode(d["content"]).decode("utf-8", errors="replace")
    lines = text.splitlines()
    lo, hi = max(0, line - 1 - ctx), min(len(lines), line + ctx)
    return [f"{i+1}: {lines[i]}" for i in range(lo, hi)]


def main():
    min_stars = 0
    if "--min-stars" in sys.argv:
        min_stars = int(sys.argv[sys.argv.index("--min-stars") + 1])

    rows = json.loads(SRC.read_text(encoding="utf-8"))
    targets = [r for r in rows if r["grade2"] in ("C", "D") and r["stars"] >= min_stars]
    print(f"待复核仓库 {len(targets)} 个")

    out, done_repo = [], set()
    if OUT.exists():
        out = json.loads(OUT.read_text(encoding="utf-8"))
        done_repo = {o["repo"] for o in out}

    for n, r in enumerate(targets, 1):
        if r["repo"] in done_repo:
            continue
        # 只取非噪声文件里的 critical/high
        picks = [f for f in r["top_findings"]
                 if f["severity"] in ("critical", "high") and not NOISE.search(f["file"])][:4]
        if not picks:
            continue
        entry = {"repo": r["repo"], "stars": r["stars"], "grade2": r["grade2"], "items": []}
        print(f"[{n}/{len(targets)}] {r['repo']}")
        for p in picks:
            try:
                ctx = fetch_context(r["repo"], p["file"].replace("\\", "/"), p["line"])
                entry["items"].append({**p, "context": ctx})
                print(f"   OK {p['rule']} {p['file']}:{p['line']}")
            except Exception as e:
                entry["items"].append({**p, "error": str(e)[:120]})
                print(f"   失败 {p['file']}:{p['line']} {str(e)[:60]}")
            time.sleep(0.6)
        out.append(entry)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n完成，共 {len(out)} 个仓库 -> {OUT}")


if __name__ == "__main__":
    main()
