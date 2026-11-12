#!/usr/bin/env python3
"""检查 SKILL.md 里每条规则的两个例子。

每条规则必须带两行例子，格式固定：

  - 差「<真实出现过的坏写法>」（`<位置>`）
  - 好「<同一件事的好写法>」（`<位置>`）

位置有两种。仓库内的写成 `tests/bad.md:5`。真实会话的写成
`2026-11-06T13-12-23:196`，文件按时间戳在 ~/.pi/agent/sessions/ 下找。

本脚本做四件事：
  1. 每条规则至少一个「差」和一个「好」。少一个就报。
  2. 每个例子的原文能在它写的位置逐字找到。「…」分开的片段按先后顺序找。
  3. 能交给检查脚本回放的规则，回放一遍：差必须命中，好必须不命中。
  4. 加粗一致性：模板占位行、R18 正文、R18 引用的仓库样本行，三处都要整句加粗，缺一处就报。

第 3 条只覆盖词表类规则（R26 R27 R28 R29 R33）和形状类检查（R7 R31 R35 R18 R38）。
其余规则靠人判，脚本只保证原文真的在那里。

退出码：0 通过，1 有违规，2 数据或路径错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check as checker  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SESSIONS = Path.home() / ".pi" / "agent" / "sessions"

RULE_HEAD = re.compile(r"^- \*\*(R\d+)[ 　]?([^*]*)\*\*")
EXAMPLE = re.compile(r"([差好])「(.+?)」（`([^`]+)`([^）]*)）")
SESSION_LOC = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}):(\d+)$")
REPO_LOC = re.compile(r"^([^:\s]+\.(?:md|py|jsonl|json)):(\d+)$")
ELLIPSIS = "…"
TEMPLATE_BOLD = re.compile(r"^\*\*首句：.+\*\*\s*$", re.M)
SAMPLE_BOLD = re.compile(r"^\s*\*\*.+\*\*\s*$")
BOLD_RULE_PHRASE = "按模板加粗"


class Bad(Exception):
    pass


def normalize(s: str) -> str:
    """逐字比对忽略空白和星号。加粗不在比对范围，由 check_bold_consistency 查原始行。"""
    return re.sub(r"[\s*]", "", s)


def session_text(loc: str, lineno: int) -> str:
    stem, _ = loc.split(":")
    files = sorted(SESSIONS.glob(f"*/{stem}*.jsonl"))
    if not files:
        raise Bad(f"找不到会话记录 {stem}")
    lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()
    if not 1 <= lineno <= len(lines):
        raise Bad(f"{stem} 只有 {len(lines)} 行，取不到第 {lineno} 行")
    obj = json.loads(lines[lineno - 1])
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def repo_text(loc: str, lineno: int, window: int = 1) -> str:
    path, _ = loc.split(":")
    p = REPO / path
    if not p.exists():
        raise Bad(f"找不到文件 {path}")
    lines = p.read_text(encoding="utf-8").splitlines()
    if not 1 <= lineno <= len(lines):
        raise Bad(f"{path} 只有 {len(lines)} 行，取不到第 {lineno} 行")
    return "\n".join(lines[lineno - 1: lineno - 1 + window])


def source_at(loc: str, window: int = 1) -> str:
    """window=1 就是那一行本身，跨行摘录才放宽到十行。"""
    m = SESSION_LOC.match(loc)
    if m:
        return normalize(session_text(loc, int(m.group(2))))
    m = REPO_LOC.match(loc)
    if m:
        return normalize(repo_text(loc, int(m.group(2)), window))
    raise Bad(f"位置写法不认：{loc}。要写成 `路径:行号` 或 `时间戳:行号`")


def in_source(quote: str, source: str) -> bool:
    """整句逐字，或「…」分开的片段按顺序出现。"""
    parts = [normalize(p) for p in quote.split(ELLIPSIS) if normalize(p)]
    if not parts:
        return False
    pos = 0
    for part in parts:
        at = source.find(part, pos)
        if at < 0:
            return False
        pos = at + len(part)
    return True


def check_bold_consistency(skill_text: str, pairs) -> list:
    """模板、R18 正文、R18 引用的仓库样本，三处加粗必须一致。缺一处就报。"""
    fails = []
    if not TEMPLATE_BOLD.search(skill_text):
        fails.append("模板首句占位行没有整句加粗。要写成 **首句：…**。")
    r18 = next((l for l in skill_text.splitlines() if l.startswith("- **R18")), "")
    if BOLD_RULE_PHRASE not in r18:
        fails.append("R18 正文缺「首句按模板加粗」。")
    for rid, line in pairs:
        if rid != "R18" or line is None:
            continue
        items = EXAMPLE.findall(" ".join(line) if isinstance(line, list) else line)
        for kind, _quote, loc, _extra in items:
            if kind != "好" or not REPO_LOC.match(loc):
                continue
            path, lineno = loc.split(":")
            try:
                raw = (REPO / path).read_text(encoding="utf-8").splitlines()[int(lineno) - 1]
            except (OSError, IndexError):
                fails.append(f"R18 好 的样本取不到：{loc}")
                continue
            if not SAMPLE_BOLD.match(raw):
                fails.append(f"R18 好 引用的样本没有整句加粗：{loc}")
    return fails


def rules_of(skill_text: str):
    """返回 [(规则号, 例子行原文或 None)]。"""
    out, cur = [], None
    for line in skill_text.splitlines():
        if line.startswith("- **R"):
            m = RULE_HEAD.match(line)
            if not m:
                raise Bad(f"规则行格式不对：{line[:40]}")
            cur = [m.group(1), []]
            out.append(cur)
        elif line.startswith(("- **", "## ", "### ")) and not line.startswith("- **R"):
            cur = None
        elif cur is not None and re.match(r"^\s{2}- [差好]「", line):
            cur[1].append(line.strip())
    seen, dup = set(), []
    for rid, _ in out:
        if rid in seen:
            dup.append(rid)
        seen.add(rid)
    if dup:
        raise Bad(f"规则号重复：{dup}")
    return [(rid, ex or None) for rid, ex in out]


def replay(rid: str, quote: str, want_hit: bool) -> str:
    """把例子回放给检查脚本。返回 ok / miss / skip。"""
    if ELLIPSIS in quote:
        return "skip"
    code = {"R7": "H1", "R31": "H2", "R35": "H3", "R18": "S1", "R38": "W3"}.get(rid)
    rules = [] if code else [r for r in checker.load_rules(checker.WORDLIST)
                             if r["rule"] == rid]
    if not code and not rules:
        return "skip"
    args = argparse.Namespace(disable=set(), no_soft=False, check_quoted=True,
                              max_steps=checker.DEFAULT_MAX_STEP,
                              max_desc=checker.DEFAULT_MAX_DESC)
    found, _ = checker.check(quote, rules, args)
    hit = (any(f["id"] == code for f in found) if code
           else any(f["rule"] == rid for f in found))
    return "ok" if hit == want_hit else "miss"


def main(argv):
    ap = argparse.ArgumentParser(description="检查 SKILL.md 每条规则的两个例子。")
    ap.add_argument("--skill", default=str(REPO / "SKILL.md"))
    ap.add_argument("--no-replay", action="store_true", help="只查原文在不在，不回放")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        skill = Path(args.skill).read_text(encoding="utf-8")
        pairs = rules_of(skill)
    except (Bad, OSError) as exc:
        print(f"数据错误：{exc}", file=sys.stderr)
        return 2
    if not pairs:
        print("没找到任何规则行。格式是不是变了？", file=sys.stderr)
        return 2

    fails, rows = [], []
    fails.extend(check_bold_consistency(skill, pairs))
    n_ex = n_verbatim = n_replayed = 0
    skipped = []
    for rid, line in pairs:
        if line is None:
            fails.append(f"{rid} 没有例子行。加两行：- 差「…」（`位置`）和 - 好「…」（`位置`）")
            rows.append({"rule": rid, "status": "no-example"})
            continue
        items = EXAMPLE.findall(" ".join(line) if isinstance(line, list) else line)
        kinds = {k for k, _, _, _ in items}
        if kinds != {"差", "好"} or len(items) < 2:
            fails.append(f"{rid} 的例子不是「差」加「好」各一条：{' '.join(line)[:40]}")
        for kind, quote, loc, extra in items:
            n_ex += 1
            try:
                ok = in_source(quote, source_at(loc, 10 if ELLIPSIS in quote else 1))
            except Bad as exc:
                fails.append(f"{rid} {kind} 的位置取不到：{exc}")
                continue
            if ok:
                n_verbatim += 1
            else:
                fails.append(f"{rid} {kind} 在 {loc} 找不到原文：{quote[:30]}")
            if "不回放" in extra:
                skipped.append(f"{rid}{kind}")
                rows.append({"rule": rid, "status": "no-replay"})
            elif not args.no_replay:
                state = replay(rid, quote, want_hit=(kind == "差"))
                if state == "miss":
                    fails.append(f"{rid} {kind} 回放不符：{kind}「{quote[:24]}」")
                elif state == "ok":
                    n_replayed += 1
        rows.append({"rule": rid, "status": "ok" if not fails else "check"})

    if args.json:
        print(json.dumps({"rules": len(pairs), "examples": n_ex, "verbatim": n_verbatim,
                          "replayed": n_replayed, "fails": fails}, ensure_ascii=False, indent=2))
    else:
        for f in fails:
            print("违规", f)
        print(f"→ 规则 {len(pairs)} 条，例子 {n_ex} 个，原文核对 {n_verbatim} 个，回放 {n_replayed} 个")
        if skipped:
            print(f"  按例子行标的不回放：{'、'.join(skipped)}")
        if not fails:
            print("每条规则都带两个能查到原文的例子。")
            print("加粗三处一致：模板占位行、R18 正文、引用样本。")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
