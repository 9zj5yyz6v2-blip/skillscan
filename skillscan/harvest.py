#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvest.py —— 真实生态测绘工具

三个子命令：
  discover  用 GitHub 搜索 API 找出真实存在的 skill / MCP 仓库
  scan      下载并批量跑分（走 codeload，不消耗 API 配额）
  report    生成分布统计 + Markdown 榜单（可直接发布）

设计原则（这是产品的立命之本）：
  1. 只读：不执行被扫描仓库的任何代码，不 pip install，不 npm install
  2. 可复现：记录 commit sha，任何人可以用同一 sha 复现同一结果
  3. 可断点续跑：results.jsonl 里已有的仓库自动跳过
  4. 礼貌：不并发爆破、失败重试有限、遵循 GitHub 速率限制

用法：
    python harvest.py discover --limit 400
    python harvest.py scan --limit 300
    python harvest.py report

有 GITHUB_TOKEN 环境变量时会自动使用（搜索限速 10/min -> 30/min）。
"""

import argparse
import io
import json
import os
import random
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skillscan import scan_path, load_rules  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "data"
REPOS_FILE = DATA / "repos.json"
# 临时解压目录放系统 temp：避免在项目目录里批量删文件触发安全拦截
WORK = Path(tempfile.gettempdir()) / "skillscan-work"
RESULTS = DATA / "results.jsonl"

UA = "skillscan-harvest/0.1 (+https://github.com/; static-analysis-only)"
MAX_TARBALL = 25 * 1024 * 1024      # 25MB，超过直接跳过（不是目标样本）
MAX_COMMITS_CACHE = 400

# 多条搜索语句合并去重，覆盖"技能"和"MCP 服务"两个真实生态
SEARCH_QUERIES = [
    "SKILL.md in:path",
    "claude skills in:name,description,readme",
    "claude-skills in:topics",
    "mcp-server in:name,description",
    "topic:mcp-server stars:>5",
    "topic:mcp stars:>20",
    "anthropic skills in:name,description,readme",
]


def _load_dotenv():
    """从项目根目录的 .env 读取配置（无需装任何库）。
    该文件已在 .gitignore 中排除，不会被提交。"""
    for p in (ROOT.parent / ".env", ROOT / ".env"):
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
        except OSError:
            pass


def _token():
    """按「最容易上手」的顺序找 token：环境变量 → .env → 项目根目录的纯文本文件。
    最后一个是为完全不懂技术的使用者准备的：用记事本新建一个文件粘贴进去即可。"""
    _load_dotenv()
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t

    # 小白友好通道：把 token 粘进一个普通文本文件，文件名用中文，记事本就能建
    for name in ("我的token.txt", "token.txt", "我的token", "TOKEN.txt"):
        for p in (ROOT.parent / name, ROOT / name):
            try:
                if p.exists():
                    v = p.read_text(encoding="utf-8", errors="ignore").strip()
                    if v.startswith("ghp_") or v.startswith("github_pat_"):
                        return v.split()[0]
            except OSError:
                pass
    return ""


def _req(url, accept="application/vnd.github+json", timeout=25):
    h = {"User-Agent": UA, "Accept": accept}
    t = _token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return urllib.request.Request(url, headers=h)


def _read_all(resp, limit=None, deadline=None):
    """分块读取。deadline 为总时长上限（秒）：慢速连接上单个响应不允许无限挂起。"""
    chunks, total = [], 0
    start = time.monotonic()
    while True:
        if deadline is not None and time.monotonic() - start > deadline:
            raise TimeoutError("read_deadline")
        try:
            b = resp.read(65536)
        except Exception:
            break
        if not b:
            break
        chunks.append(b)
        total += len(b)
        if limit and total > limit:
            break
    return b"".join(chunks)


def _get_json(url, timeout=20, total=40):
    """带重试与总时长上限：代理偶发断流会导致 JSON 截断，慢响应不允许无限挂起。"""
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(_req(url), timeout=timeout) as r:
                raw = _read_all(r, deadline=total)
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            last = e
            time.sleep(2 * (attempt + 1))
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- discover

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TOPICS = ["mcp-server", "mcp", "claude-skills", "claude-code",
          "ai-agents", "agent-skills", "llm-tools", "ai-tools"]

_HREF_RX = re.compile(r'href="(/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"')
_OFFICIAL = {"github", "topics", "login", "features", "sponsors", "collections",
             "trending", "orgs", "about", "pricing", "marketplace", "settings",
             "notifications", "new", "explore", "apps", "enterprise", "security"}


def discover_html(pages=5):
    """不消耗 API 配额：抓公开主题页，逐页解析真实仓库名与 star 数。"""
    DATA.mkdir(parents=True, exist_ok=True)
    found, seen = [], set()

    for topic in TOPICS:
        for page in range(1, pages + 1):
            url = f"https://github.com/topics/{topic}?page={page}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml",
                })
                with urllib.request.urlopen(req, timeout=30) as r:
                    html = _read_all(r, limit=3_000_000).decode("utf-8", "ignore")
            except Exception as e:
                print(f"  [{topic} p{page}] 失败 {e}", file=sys.stderr)
                break

            names = []
            for m in _HREF_RX.finditer(html):
                p = m.group(1)
                if p.count("/") != 2:
                    continue
                owner = p.strip("/").split("/")[0]
                if owner.lower() in _OFFICIAL:
                    continue
                if p not in names:
                    names.append(p)

            new = 0
            for p in names:
                full = p.strip("/")
                if full in seen:
                    continue
                seen.add(full)
                found.append({
                    "repo": full,
                    "url": f"https://github.com/{full}",
                    "desc": "",
                    "stars": 0,
                    "forks": 0,
                    "lang": None,
                    "license": None,
                    "branch": None,       # 未知，扫描时按 main/master 依次尝试
                    "pushed_at": None,
                    "archived": False,
                    "topics": [topic],
                })
                new += 1
            print(f"  [{topic} p{page}] +{new} / 累计 {len(found)}")
            if new == 0:
                break
            time.sleep(random.uniform(0.8, 1.6))

    skip_kw = ["awesome", "tutorial", "course", "interview", "cheatsheet",
               "roadmap", "handbook", "book"]
    clean = [r for r in found
             if not any(k in (r["repo"] + " " + r["desc"]).lower() for k in skip_kw)]

    REPOS_FILE.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "topics-html",
        "total_found": len(found),
        "total_kept": len(clean),
        "repos": clean,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n发现 {len(found)} 个，过滤后保留 {len(clean)} 个 -> {REPOS_FILE}")
    return clean


def discover_api(limit):
    DATA.mkdir(parents=True, exist_ok=True)
    found, seen = [], set()

    for q in SEARCH_QUERIES:
        if len(found) >= limit:
            break
        page = 1
        while page <= 2 and len(found) < limit:
            params = urllib.parse.urlencode({
                "q": q, "sort": "stars", "order": "desc",
                "per_page": 100, "page": page,
            })
            url = f"https://api.github.com/search/repositories?{params}"
            try:
                d = _get_json(url)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    print(f"  [{q}] 被限速，跳过该查询", file=sys.stderr)
                    break
                print(f"  [{q}] 失败 {e.code}", file=sys.stderr)
                break
            except Exception as e:
                print(f"  [{q}] 异常 {e}", file=sys.stderr)
                break

            items = d.get("items") or []
            if not items:
                break
            new = 0
            for it in items:
                full = it.get("full_name")
                if not full or full in seen:
                    continue
                seen.add(full)
                found.append({
                    "repo": full,
                    "url": it.get("html_url"),
                    "desc": (it.get("description") or "")[:200],
                    "stars": it.get("stargazers_count", 0),
                    "forks": it.get("forks_count", 0),
                    "lang": it.get("language"),
                    "license": (it.get("license") or {}).get("spdx_id"),
                    "branch": it.get("default_branch") or "main",
                    "pushed_at": it.get("pushed_at"),
                    "archived": it.get("archived", False),
                    "topics": it.get("topics") or [],
                })
                new += 1
                if len(found) >= limit:
                    break
            print(f"  [{q}] p{page} +{new} / 累计 {len(found)}")
            if new == 0:
                break
            page += 1
            time.sleep(7 if not _token() else 2.5)

    skip_kw = ["awesome", "tutorial", "course", "interview", "cheatsheet"]
    clean = [r for r in found
             if not any(k in (r["repo"] + " " + r["desc"]).lower() for k in skip_kw)]

    REPOS_FILE.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "search-api",
        "total_found": len(found),
        "total_kept": len(clean),
        "repos": clean,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n发现 {len(found)} 个，过滤后保留 {len(clean)} 个 -> {REPOS_FILE}")
    return clean


# ---------------------------------------------------------------- scan

def _done_set():
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["repo"])
            except Exception:
                pass
    return done


def _fetch_tarball(full, branch=None):
    """优先用指定分支；未知分支时依次试 main / master。codeload 不消耗 API 配额。
    注意：不携带 Authorization 头（下载公开 tarball 不需要凭据，也不应把 token 发到这里）。
    本机代理对连续请求会间歇性返回 502，因此带重试与退避。"""
    branches = [branch] if branch else []
    branches += [b for b in ("main", "master") if b not in branches]
    last = None
    for b in branches:
        url = f"https://codeload.github.com/{full}/tar.gz/refs/heads/{b}"
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Accept": "application/gzip",
                })
                with urllib.request.urlopen(req, timeout=90) as r:
                    cl = r.headers.get("Content-Length")
                    if cl and int(cl) > MAX_TARBALL:
                        raise ValueError(f"too_large:{int(cl)//1024//1024}MB")
                    buf = _read_all(r, limit=MAX_TARBALL)
                    if len(buf) > MAX_TARBALL:
                        raise ValueError("too_large:stream")
                    return buf, b
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 404:
                    break                      # 分支不存在，换下一个分支
                if e.code in (502, 503, 429):
                    time.sleep(8 * (attempt + 1))   # 代理/限速，退避后重试
                    continue
                raise
            except ValueError:
                raise
            except Exception as e:
                last = e
                time.sleep(5 * (attempt + 1))
                continue
    raise last or ValueError("no_branch")


def _extract(buf, dest):
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(buf), mode="r:gz") as tf:
        members = []
        for m in tf.getmembers():
            if not m.isfile():
                continue
            if m.size > 2 * 1024 * 1024:
                continue
            parts = m.name.split("/")
            # 跳过 .git / node_modules / 大目录
            if any(p in (".git", "node_modules", ".venv", "venv", "__pycache__",
                         "dist", "build", ".next", "vendor") for p in parts):
                continue
            total += m.size
            if total > 30 * 1024 * 1024:
                break
            parts = parts[1:]          # 去掉顶层目录
            if not parts:
                continue
            m.name = "/".join(parts)
            members.append(m)
        try:
            tf.extractall(dest, members=members, filter="data")
        except TypeError:
            tf.extractall(dest, members=members)
    return dest


SCAN_TEXT_EXT = {".md", ".markdown", ".txt", ".py", ".sh", ".bash", ".js", ".mjs",
                 ".cjs", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml",
                 ".rb", ".go", ".rs", ".php", ".ps1"}
SCAN_SKIP_PARTS = {"node_modules", ".git", "dist", "build", ".next", "vendor",
                   "__pycache__", ".venv", "venv", "testdata", "fixtures", "test", "tests"}
MAX_FILES_PER_REPO = 18        # 每仓库最多取多少个文本文件（控制 API 配额）
MAX_BLOB_BYTES = 400 * 1024


def _should_scan(path_str, size):
    p = path_str.replace("\\", "/")
    parts = p.split("/")
    if any(x in parts for x in SCAN_SKIP_PARTS):
        return False
    if Path(p).suffix.lower() not in SCAN_TEXT_EXT:
        return False
    return size is None or size <= MAX_BLOB_BYTES


def scan_via_api(limit):
    """不下载 tarball：用 git trees + contents API 逐个读文本内容。
    背景：本机网络代理对 codeload.github.com（tarball 下载域名）间歇性 502，
    但 api.github.com 稳定。代价是消耗核心配额（每仓库约 1 + N 次，N≤18），
    认证后 5000/时，足够支撑数百仓库。"""
    import base64
    if not REPOS_FILE.exists():
        print("先跑 discover", file=sys.stderr)
        return
    pool = json.loads(REPOS_FILE.read_text(encoding="utf-8"))["repos"]
    done = _done_set()
    todo = [r for r in pool if r["repo"] not in done][:limit]
    print(f"待扫 {len(todo)} 个（已完成 {len(done)}）· 通道：API 文件读取\n", flush=True)

    rules = load_rules()
    WORK.mkdir(parents=True, exist_ok=True)
    ok = fail = 0

    for i, r in enumerate(todo, 1):
        full = r["repo"]
        safe = full.replace("/", "__")
        dest = WORK / safe
        tag = f"[{i}/{len(todo)}] {full}"
        try:
            if dest.exists():
                _cleanup_force(dest)
            dest.mkdir(parents=True, exist_ok=True)

            # 1) 文件树
            branch = r.get("branch") or "main"
            tree = _get_json(
                f"https://api.github.com/repos/{full}/git/trees/{branch}?recursive=1")
            blobs = [t for t in tree.get("tree", [])
                     if t.get("type") == "blob" and _should_scan(t.get("path", ""), t.get("size"))]
            blobs.sort(key=lambda t: (0 if Path(t["path"]).name == "SKILL.md" else
                                      1 if Path(t["path"]).name.lower().startswith("readme") else 2,
                                      t.get("path", "")))
            blobs = blobs[:MAX_FILES_PER_REPO]

            # 2) 逐个读内容
            got = 0
            for b in blobs:
                try:
                    meta = _get_json(
                        "https://api.github.com/repos/{}/contents/{}?ref={}".format(
                            full, urllib.parse.quote(b["path"]), branch))
                    data = meta.get("content")
                    if not data:
                        continue
                    raw = base64.b64decode(data)
                    out = dest / b["path"]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(raw)
                    got += 1
                except Exception:
                    continue
            if got == 0:
                raise ValueError("no_readable_files")

            # 3) 扫描
            res = scan_path(str(dest), rules)
            rec = {
                "repo": full, "url": r["url"], "desc": r["desc"],
                "stars": r["stars"], "license": r["license"],
                "pushed_at": r["pushed_at"],
                "score": res["score"], "grade": res["grade"],
                "counts": res["counts"], "files_scanned": res["files_scanned"],
                "ruleset_version": res["ruleset_version"],
                "via": "api-content",
                "top_findings": [
                    {"rule": h["rule"], "severity": h["severity"],
                     "category": h["category_name"], "file": h["file"],
                     "line": h["line"], "message": h["message"]}
                    for h in sorted(res["findings"],
                                    key=lambda x: {"critical": 0, "high": 1,
                                                   "medium": 2, "low": 3}.get(x["severity"], 9))[:8]
                ],
                "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with RESULTS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            c = rec["counts"]
            print(f"{tag}  {rec['grade']} {rec['score']:>3}  "
                  f"(c{c['critical']}/h{c['high']}/m{c['medium']})  "
                  f"{got}/{len(blobs)}f", flush=True)
            ok += 1
            _cleanup_force(dest)

        except urllib.error.HTTPError as e:
            fail += 1
            msg = "rate_limited" if e.code in (403, 429) else f"http_{e.code}"
            print(f"{tag}  跳过: {msg}", flush=True)
            if e.code in (403, 429):
                print("配额用尽，停止本轮。已完成的仍在 results.jsonl。", flush=True)
                break
            _cleanup_force(dest)
        except Exception as e:
            fail += 1
            print(f"{tag}  跳过: {e}", flush=True)
            _cleanup_force(dest)
        time.sleep(random.uniform(0.6, 1.4))

    print(f"\n完成 {ok} 个，跳过 {fail} 个。累计 {len(done) + ok}", flush=True)


def _cleanup_force(dest):
    """尽力删除；本环境可能拦截批量删除，失败则忽略（目录在系统 temp 下，可留给系统回收）。"""
    try:
        shutil.rmtree(dest, ignore_errors=True)
    except Exception:
        pass


def scan_batch(limit):
    if not REPOS_FILE.exists():
        print("先跑 discover", file=sys.stderr)
        return
    pool = json.loads(REPOS_FILE.read_text(encoding="utf-8"))["repos"]
    done = _done_set()
    todo = [r for r in pool if r["repo"] not in done][:limit]
    print(f"待扫 {len(todo)} 个（已完成 {len(done)}）\n", flush=True)

    rules = load_rules()
    WORK.mkdir(parents=True, exist_ok=True)
    ok = fail = 0

    for i, r in enumerate(todo, 1):
        full = r["repo"]
        safe = full.replace("/", "__")
        dest = WORK / safe
        tag = f"[{i}/{len(todo)}] {full}"
        try:
            if not dest.exists():
                buf, br = _fetch_tarball(full, r.get("branch"))
                _extract(buf, dest)
                r["branch"] = br

            res = scan_path(str(dest), rules)
            rec = {
                "repo": full,
                "url": r["url"],
                "desc": r["desc"],
                "stars": r["stars"],
                "license": r["license"],
                "pushed_at": r["pushed_at"],
                "score": res["score"],
                "grade": res["grade"],
                "counts": res["counts"],
                "files_scanned": res["files_scanned"],
                "ruleset_version": res["ruleset_version"],
                "top_findings": [
                    {"rule": h["rule"], "severity": h["severity"],
                     "category": h["category_name"], "file": h["file"],
                     "line": h["line"], "message": h["message"]}
                    for h in sorted(res["findings"],
                                    key=lambda x: {"critical": 0, "high": 1,
                                                   "medium": 2, "low": 3}.get(x["severity"], 9))[:8]
                ],
                "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with RESULTS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            c = rec["counts"]
            print(f"{tag}  {rec['grade']} {rec['score']:>3}  "
                  f"(c{c['critical']}/h{c['high']}/m{c['medium']})  {rec['files_scanned']}f",
                  flush=True)
            ok += 1
            _cleanup_force(dest)

        except Exception as e:
            fail += 1
            print(f"{tag}  跳过: {e}", flush=True)
            _cleanup_force(dest)
        time.sleep(random.uniform(2.0, 3.5))   # 代理对高频请求会 502，放慢到 2–3.5 秒

    print(f"\n完成 {ok} 个，跳过 {fail} 个。累计 {len(done) + ok}", flush=True)


# ---------------------------------------------------------------- report

def load_results():
    if not RESULTS.exists():
        return []
    out = []
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def report(top_n=25):
    rows = load_results()
    if not rows:
        print("没有结果", file=sys.stderr)
        return

    rows.sort(key=lambda x: (-x["score"], -x["stars"]))
    n = len(rows)
    dist = {}
    for r in rows:
        dist.setdefault(r["grade"], []).append(r)
    sev = {k: sum(r["counts"][k] for r in rows) for k in ("critical", "high", "medium", "low")}
    clean = len(dist.get("A", []))
    risky = [r for r in rows if r["grade"] in ("C", "D")]

    lines = []
    add = lines.append
    add(f"# Agent Skills / MCP 供应链风险测绘 · 第 1 期")
    add("")
    add(f"> 样本：GitHub 上真实存在的 **{n}** 个 skill / MCP 仓库　"
        f"规则集：v{rows[0]['ruleset_version']}　"
        f"扫描时间：{time.strftime('%Y-%m-%d')}")
    add("")
    add("## 一句话结论")
    add("")
    add(f"{n} 个样本里，**{len(dist.get('A', []))} 个（{clean/n*100:.0f}%）在规则集范围内没发现问题**，"
        f"**{len(risky)} 个（{len(risky)/n*100:.0f}%）存在至少一处高危或严重命中**。"
        f"累计命中 critical {sev['critical']} 次、high {sev['high']} 次。")
    add("")
    add("## 分布")
    add("")
    add("| 评级 | 含义 | 数量 | 占比 |")
    add("|---|---|---|---|")
    meaning = {"A": "未发现问题", "B": "低风险", "C": "需人工复核", "D": "不建议直接安装"}
    for g in ["A", "B", "C", "D"]:
        c = len(dist.get(g, []))
        add(f"| {g} | {meaning[g]} | {c} | {c/n*100:.1f}% |")
    add("")

    if risky:
        add(f"## 需要关注的 {len(risky)} 个（按风险分排序，前 {min(top_n, len(risky))} 个）")
        add("")
        add("| 仓库 | 评级 | 分 | 严重/高危 | Stars | 主要命中 |")
        add("|---|---|---|---|---|---|")
        for r in risky[:top_n]:
            top = r["top_findings"][0] if r["top_findings"] else {}
            cat = top.get("category", "")
            rule = top.get("rule", "")
            add(f"| [{r['repo']}]({r['url']}) | {r['grade']} | {r['score']} | "
                f"{r['counts']['critical']}/{r['counts']['high']} | {r['stars']} | "
                f"{cat} · {rule} |")
        add("")
        add("> 说明：以上均为**静态规则命中**，不代表仓库作者有恶意。"
            "发布前必须逐条人工复核——这是本项目的核心纪律。")
        add("")

    # 按类别统计最常命中的风险类型
    cat_count = {}
    for r in rows:
        for f in r["top_findings"]:
            cat_count[f["category"]] = cat_count.get(f["category"], 0) + 1
    if cat_count:
        add("## 最常出现的风险类型")
        add("")
        add("| 类别 | 出现次数 |")
        add("|---|---|")
        for k, v in sorted(cat_count.items(), key=lambda x: -x[1])[:10]:
            add(f"| {k} | {v} |")
        add("")

    add("## 方法与边界")
    add("")
    add("- 纯静态扫描：不执行任何被扫描仓库的代码，不安装依赖，不联网回传")
    add("- 每条规则可归因至公开标准（CWE / OWASP LLM Top 10 / MITRE ATLAS）")
    add("- 文档（.md）中的命令类命中已降权，引用块忽略——避免把\"安装说明\"误判为\"恶意行为\"")
    add("- **A 级的含义是\"在本文规则集范围内没发现问题\"，不等于安全**")
    add("")

    text = "\n".join(lines)
    out_md = ROOT.parent / "content" / "issue-001.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(text, encoding="utf-8")
    print(text[:1500])
    print(f"\n...\n完整榜单已写入 {out_md}")
    print(f"统计：{n} 个样本 / A {clean} / B {len(dist.get('B',[]))} "
          f"/ C {len(dist.get('C',[]))} / D {len(dist.get('D',[]))}")


def main():
    ap = argparse.ArgumentParser(description="真实 skill/MCP 生态测绘")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="搜索真实仓库")
    d.add_argument("--limit", type=int, default=400)
    d.add_argument("--source", choices=["auto", "api", "html"], default="auto",
                   help="api=官方搜索(需 token 才稳) / html=公开主题页(无需 token)")

    s = sub.add_parser("scan", help="下载并批量跑分")
    s.add_argument("--limit", type=int, default=300)
    s.add_argument("--via", choices=["tarball", "api"], default="api",
                   help="api=逐文件读取（稳，消耗配额）/ tarball=打包下载（需直连 codeload）")

    r = sub.add_parser("report", help="生成榜单")
    r.add_argument("--top", type=int, default=25)

    a = ap.parse_args()
    if a.cmd == "discover":
        src = a.source
        if src == "auto":
            src = "api" if _token() else "html"
        print(f"数据通道：{src}{'（已带 token）' if _token() else '（无 token）'}\n")
        if src == "api":
            discover_api(a.limit)
        else:
            discover_html(pages=max(3, a.limit // 60))
    elif a.cmd == "scan":
        if a.via == "api":
            scan_via_api(a.limit)
        else:
            scan_batch(a.limit)
    else:
        report(a.top)


if __name__ == "__main__":
    main()
