#!/usr/bin/env python3
"""ant-output-rule 的例子采集器。

按正则扫真实文本，打印命中的句子和它的位置。SKILL.md 每条规则下面的
「改」「留」两条例子都由这个脚本找出，不靠人想。

扫两处来源：
  本仓库的 Markdown 文件（默认 SKILL.md README.md references examples tests）
  会话记录（默认 ~/.pi/agent/sessions 下 cwd 是本仓库上级目录的那些，只取助手输出的正文）

输出第一列可以直接写进例子当来源。会话记录里的一行可能很长，所以先按句拆开。

用法：
  python3 scripts/harvest.py '；'                 找分号连动作（R31）
  python3 scripts/harvest.py '赋能|闭环|抓手'       找词表命中（R26 R29）
  python3 scripts/harvest.py '我先.{0,20}然后'     找过程叙述（R21）
  python3 scripts/harvest.py '；' --repo-only     只扫仓库
  python3 scripts/harvest.py '；' --sessions-only 只扫会话记录
  python3 scripts/harvest.py '.' --role user --chars 8-30  找用户原话里 8 到 30 字的句子
  python3 scripts/harvest.py '；' --limit 40

退出码：0 有命中，1 没有命中，2 参数或路径错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check import count_chars  # noqa: E402  字数口径与检查脚本一致

REPO_DEFAULT = Path(__file__).resolve().parent.parent
SESSION_ROOT = Path.home() / ".pi" / "agent" / "sessions"
REPO_FILES = ["SKILL.md", "README.md", "references", "examples", "tests"]
SENT_END = re.compile(r"(?<=[。！？!?])")
SKIP_LINE = re.compile(r"^\s*(```|\||#{1,6}\s|<!--)")


def related(path: str, workspace: Path) -> bool:
    """会话的 cwd 必须落在工作区内，避免读别的项目。"""
    try:
        Path(path).resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def repo_default_workspace() -> Path:
    return REPO_DEFAULT.parent


def repo_texts(root: Path):
    for name in REPO_FILES:
        p = root / name
        if p.is_file() and p.suffix == ".md":
            yield p, p.read_text(encoding="utf-8")
        elif p.is_dir():
            for f in sorted(p.rglob("*.md")):
                yield f, f.read_text(encoding="utf-8")


def session_texts(sessions: Path, workspace: Path, role: str = "assistant"):
    if not sessions.is_dir():
        return
    for f in sorted(sessions.rglob("*.jsonl")):
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            continue
        try:
            head = json.loads(lines[0])
        except json.JSONDecodeError:
            continue
        cwd = head.get("cwd") or (head.get("payload") or {}).get("cwd")
        if not cwd or not related(cwd, workspace):
            continue
        for lineno, raw in enumerate(lines, 1):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") or {}
            if role != "both" and msg.get("role") != role:
                continue
            content = msg.get("content")
            if isinstance(content, str):
                yield f, lineno, content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        yield f, lineno, block.get("text", "")


def sentences(text: str):
    for part in SENT_END.split(text):
        s = part.strip()
        if s:
            yield s


def clip(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def main(argv):
    ap = argparse.ArgumentParser(description="按正则从真实文本里取例子。")
    ap.add_argument("patterns", nargs="+", help="正则，命中任意一条就算")
    ap.add_argument("--repo", default=str(REPO_DEFAULT))
    ap.add_argument("--sessions", default=str(SESSION_ROOT))
    ap.add_argument("--role", default="assistant", choices=["assistant", "user", "both"],
                    help="扫谁的话。用户原话是「好」例子的首选来源")
    ap.add_argument("--workspace", default=str(repo_default_workspace()),
                    help="会话说 cwd 落在这个目录下才扫，默认仓库的上级目录")
    ap.add_argument("--repo-only", action="store_true", help="不扫会话记录")
    ap.add_argument("--sessions-only", action="store_true", help="只扫会话记录")
    ap.add_argument("--limit", type=int, default=20, help="最多打印多少条")
    ap.add_argument("--max-len", type=int, default=120, help="句子超过这个字数就截断")
    ap.add_argument("--chars", help="只留字数在区间内的句子，写法如 38-40，口径同 check.py")
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    _ = repo
    if not repo.is_dir():
        print(f"找不到仓库：{repo}", file=sys.stderr)
        return 2
    lo, hi = 0, 10 ** 6
    if args.chars:
        try:
            lo, hi = (int(x) for x in args.chars.split("-"))
        except ValueError:
            print("--chars 要写成 38-40", file=sys.stderr)
            return 2
    try:
        pats = [re.compile(p) for p in args.patterns]
    except re.error as exc:
        print(f"正则无效：{exc}", file=sys.stderr)
        return 2

    hits = 0
    if not args.sessions_only:
        for path, text in repo_texts(repo):
            for lineno, line in enumerate(text.splitlines(), 1):
                if SKIP_LINE.match(line):
                    continue
                for sent in sentences(line):
                    if not lo <= count_chars(sent) <= hi:
                        continue
                    if any(p.search(sent) for p in pats):
                        print(f"{path.relative_to(repo)}:{lineno}\t{clip(sent, args.max_len)}")
                        hits += 1
                        if hits >= args.limit:
                            return 0

    if not args.repo_only:
        for path, lineno, text in session_texts(Path(args.sessions), Path(args.workspace), args.role):
            for line in text.splitlines():
                for sent in sentences(line):
                    if not lo <= count_chars(sent) <= hi:
                        continue
                    if any(p.search(sent) for p in pats):
                        print(f"{path.name}:{lineno}\t{clip(sent, args.max_len)}")
                        hits += 1
                        if hits >= args.limit:
                            return 0

    if not hits:
        print("没有命中。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
