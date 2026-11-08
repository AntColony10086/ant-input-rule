#!/usr/bin/env python3
"""ant-output-rule 的确定性检查器。

只判能机械判对错的部分。报告是候选，不是判决。

查五类内置检查：
  H1  句子超长。中文按 R7 字数，英文按 20/25 词（R7）
  H2  分号串动作（R31）
  H3  两层的层序与节序（R35）
  W2  一段内两次破折号插入（R32 的近似，只报）
  S1  首句不是三种句子之一（R18，默认开启，用 --disable S1 关掉）

再按 scripts/wordlist.jsonl 查词表与句式（R26 R27 R28 R29 R33）。
词表条目的 rule 字段指向对应规则号，id 稳定，可给 --disable 用。

不做：
  R30 无生命主语、R32 主谓距离、R34 定语套叠：不可机械判。
  词表里的「硬凑搭配」只覆盖常见写法，长尾判不了。
  状态词的语义正确性：只查白名单，查不了用得对不对。

退出码：0 通过，1 有硬违规，2 用法或数据错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STATUS_WORDS = [
    "已完成", "部分完成", "未开始", "进行中", "已验证",
    "未验证", "失败", "跳过", "阻塞", "未确认",
]

WORDLIST = Path(__file__).resolve().parent / "wordlist.jsonl"
DEFAULT_MAX_STEP = 30
DEFAULT_MAX_DESC = 40

TOKEN = re.compile(r"`[^`]*`|[A-Za-z0-9_.\-/]+|\S")
CJK = re.compile(r"[\u4e00-\u9fff]")
ASCII_WORD = re.compile(r"^[A-Za-z0-9_.\-/]+$")
SENT_END = re.compile(r"[。！？!?]+")
STEP_HEAD = re.compile(r"^\s*\d+[.、)]\s+")
TABLE_ROW = re.compile(r"^\s*\|")
HEADING = re.compile(r"^\s*#{1,6}\s")
COMMENT_LINE = re.compile(r"^\s*(#|//|--)\s?(.*)$")
COMMENT_TAIL = re.compile(r"(?<![:\w])\s(#|//)\s+(\S.*)$")
QUOTED = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”")
LAYER = re.compile(r"^##\s+(接下来|已完成|结论|答复)\s*$")
TIP_HEAD = re.compile(r"^\s*[-*+]\s+\*\*(待确认|下一步|后续|不做)\*\*")
DONE_HEAD = re.compile(r"^\s*\*\*(改动|验证|现象|原因|证据)\*\*")
SECOND_LAYERS = ["已完成", "结论", "答复"]
TIP_ORDER = ["待确认", "下一步", "后续", "不做"]
DONE_ORDER = ["改动", "验证", "现象", "原因", "证据"]
FIRST_HEADS = STATUS_WORDS + ["结论", "原因", "待确认"]

TIP_ONLY = "## 接下来\n\n- **不做**：iOS 实测 — 没有环境。\n"
INLINE_CASES = [
    ("这个方案可行。\n\n" + TIP_ONLY, {"S1"}),
    ("已完成：改动完成。\n\n" + TIP_ONLY + "\n## 结论\n\n**证据**：\n\n- `a.ts:1` — 说明。\n", set()),
    ("已完成：改动完成。\n\n## 结论\n\n**原因**：说明。\n\n" + TIP_ONLY, {"H3"}),
    ("已完成：改动完成。\n\n## 结论\n\n**原因**：说明。\n\n## 答复\n\n**证据**：\n\n- `a.ts:1` — 说明。\n", {"H3"}),
]


class DataError(Exception):
    pass


def off(disabled, code_id, rule_no):
    """--disable 同时认检查编号和规则号。"""
    return code_id in disabled or rule_no in disabled


def load_rules(path):
    if not path.exists():
        raise DataError(f"找不到词表：{path}")
    rules = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DataError(f"{path}:{i} 不是合法 JSON：{exc.msg}")
        missing = {"id", "level", "kind", "value", "rule"} - set(obj)
        if missing:
            raise DataError(f"{path}:{i} 缺字段：{sorted(missing)}")
        if obj["kind"] == "pattern":
            try:
                obj["_re"] = re.compile(obj["value"])
            except re.error as exc:
                raise DataError(f"{path}:{i} 正则无效：{exc}")
        if obj.get("exclude"):
            try:
                obj["_ex"] = [re.compile(x) for x in obj["exclude"]]
            except re.error as exc:
                raise DataError(f"{path}:{i} exclude 正则无效：{exc}")
        rules.append(obj)
    if not rules:
        raise DataError(f"{path} 是空的")
    return rules


def extract_regions(text):
    """切成要检查的片段：正文行，以及代码块里的注释。

    返回 [(行号, 文本, 类型)]，类型是 prose 或 comment。
    """
    out = []
    fence = None
    fence_start = None
    block = None
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if fence:
                fence = None
            else:
                fence = stripped[:3]
                fence_start = lineno
            block = None
            continue
        if not fence:
            out.append((lineno, line, "prose"))
            continue
        s = line.strip()
        if not s:
            continue
        if block:
            out.append((lineno, s, "comment"))
            if block in s:
                block = None
            continue
        if s.startswith("/*"):
            out.append((lineno, s, "comment"))
            if "*/" not in s:
                block = "*/"
            continue
        if s.startswith("<!--"):
            out.append((lineno, s, "comment"))
            if "-->" not in s:
                block = "-->"
            continue
        m = COMMENT_LINE.match(line)
        if m and m.group(2).strip():
            out.append((lineno, m.group(2), "comment"))
            continue
        m = COMMENT_TAIL.search(line)
        if m and m.group(2).strip():
            out.append((lineno, m.group(2), "comment"))
    return out, (fence_start if fence else None)


def strip_noise(s, skip_quoted=True):
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"`[^`]*`", "X", s)
    if skip_quoted:
        s = QUOTED.sub("", s)
    return s


def count_chars(s):
    """按 R7 计数：汉字 1，英文单词 1，数字 1，代码标识符 1，标点 0。"""
    n = 0
    for tok in TOKEN.findall(s):
        if ASCII_WORD.match(tok):
            n += 1
        elif CJK.match(tok):
            n += 1
    return n


def is_english(s):
    """按汉字与英文词的比例判断，允许代码标识符混在中文里。"""
    cjk = len(CJK.findall(s))
    words = sum(1 for tok in TOKEN.findall(s) if ASCII_WORD.match(tok))
    return words >= 3 and words > cjk * 2


def sentences(line):
    parts, buf = [], ""
    for ch in line:
        buf += ch
        if ch in "。！？!?":
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    return [p for p in (x.strip() for x in parts) if p]


def add(found, id_, rule, line, col, severity, message):
    found.append({
        "id": id_,
        "rule": rule,
        "line": line,
        "col": col,
        "severity": severity,
        "message": message,
    })


def check(text, rules, args):
    found = []
    skipped_comments = 0
    skipped_code_lines = 0
    regions, unclosed = extract_regions(text)

    # 统计被跳过的纯代码行，用于结尾提示
    total_lines = len(text.splitlines())
    checked_lines = {ln for ln, _, _ in regions}
    skipped_code_lines = total_lines - len(checked_lines)

    for lineno, raw, kind in regions:
        s = strip_noise(raw, not args.check_quoted)
        if not s.strip():
            continue
        if kind == "comment":
            skipped_comments += 1

        for rule in rules:
            if rule["id"] in args.disable or rule["rule"] in args.disable:
                continue
            if rule["level"] == "soft" and args.no_soft:
                continue
            hits = []
            if rule["kind"] == "word":
                start = s.find(rule["value"])
                if start >= 0:
                    hits.append((start, start + len(rule["value"])))
            else:
                hits = [(m.start(), m.end()) for m in rule["_re"].finditer(s)]
            for start, end in hits:
                if any(x.search(s, max(0, start - 6), end + 6) for x in rule.get("_ex", [])):
                    continue
                what = (f"命中「{rule['value']}」" if rule["kind"] == "word"
                        else f"命中句式 /{rule['value']}/")
                add(found, rule["id"], rule["rule"], lineno, start + 1, rule["level"], what)

        if kind != "prose":
            continue
        if HEADING.match(raw) or TABLE_ROW.match(raw):
            continue

        if not off(args.disable, "H2", "R31"):
            pos = s.find("；")
            if pos >= 0:
                add(found, "H2", "R31", lineno, pos + 1, "hard",
                    "分号把两个动作压进一句。拆成两句。")

        if not off(args.disable, "H1", "R7"):
            english = is_english(s)
            if english:
                limit, unit = (20 if STEP_HEAD.match(raw) else 25), "词"
            else:
                limit, unit = (args.max_steps if STEP_HEAD.match(raw) else args.max_desc), "字"
            for sent in sentences(s):
                n = count_chars(sent)
                if n > limit:
                    add(found, "H1", "R7", lineno, 1, "hard",
                        f"句子 {n} {unit}（上限 {limit}）。拆开，或删掉不必要的部分。")

        if not off(args.disable, "W2", "R32") and s.count("——") >= 2:
            add(found, "W2", "R32", lineno, s.find("——") + 1, "soft",
                "一段里出现两次破折号插入。移到句首或句尾。")

    if not off(args.disable, "H3", "R35"):
        add_section_order(found, text)

    if not off(args.disable, "S1", "R18"):
        first = next((r for r in regions
                      if r[2] == "prose" and r[1].strip()
                      and not HEADING.match(r[1]) and not TABLE_ROW.match(r[1])), None)
        if first is None:
            add(found, "S1", "R18", 1, 1, "hard", "正文是空的。")
        else:
            body = re.sub(r"^[\s>#*_`+\-]+", "", strip_noise(first[1], not args.check_quoted))
            if not any(body.startswith(w) for w in FIRST_HEADS):
                add(found, "S1", "R18", first[0], 1, "hard",
                    "首句不是三种句子之一。以状态词、「结论」、「原因」或「待确认」开头。")

    found.sort(key=lambda f: (f["line"], f["col"], f["id"]))
    stats = {"checked_lines": len(checked_lines), "code_lines_skipped": skipped_code_lines,
             "comment_regions": skipped_comments, "unclosed_fence": unclosed}
    return found, stats


def add_section_order(found, text):
    """两层的层序与节序。只在这类层出现时才判。"""
    seen = []
    for lineno, line in enumerate(text.splitlines(), 1):
        m = LAYER.match(line) or TIP_HEAD.match(line) or DONE_HEAD.match(line)
        if not m:
            continue
        name = m.group(1)
        if not any(n == name for _, n in seen):
            seen.append((lineno, name))
    layers = [(ln, n) for ln, n in seen if n == "接下来" or n in SECOND_LAYERS]
    if not layers:
        return
    if len(layers) > 2:
        add(found, "H3", "R35", layers[2][0], 1, "hard",
            "只允许两层。多出来的层删掉，内容并入第二层。")
    if len(layers) == 2 and layers[0][1] != "接下来":
        add(found, "H3", "R35", layers[0][0], 1, "hard",
            f"层序反了。先写「接下来」，再把「{layers[0][1]}」放到第二层。")
    for group, label in ((TIP_ORDER, "「接下来」"), (DONE_ORDER, "第二层")):
        seq = [(ln, n) for ln, n in seen if n in group]
        got = [n for _, n in seq]
        if got != sorted(got, key=group.index):
            bad = next(ln for ln, n in seq
                       if seq.index((ln, n)) > 0 and group.index(n) < group.index(got[seq.index((ln, n)) - 1]))
            add(found, "H3", "R35", bad, 1, "hard",
                f"{label}的节序反了。按 {' → '.join(group)} 排列。")


def report(found, stats, path, args, out):
    hard = [f for f in found if f["severity"] == "hard"]
    soft = [f for f in found if f["severity"] == "soft"]
    for f in found:
        out.write(f"{path}:{f['line']}:{f['col']} {f['id']} {f['rule']} {f['message']}\n")
    out.write(f"→ 硬违规 {len(hard)} 处，提示 {len(soft)} 处\n")
    if stats["code_lines_skipped"]:
        out.write(f"  跳过了 {stats['code_lines_skipped']} 行代码，"
                  f"{stats['comment_regions']} 处注释按散文检查\n")
    out.write("候选，不是判决。soft 项要人判断，不要为了消掉它改内容。\n")
    return len(hard) > 0


def selftest(root):
    clean = root / "clean.md"
    bad = root / "bad.md"
    if not clean.exists() or not bad.exists():
        print(f"缺少样本：{clean} 或 {bad}", file=sys.stderr)
        return 2
    rules = load_rules(WORDLIST)
    args = argparse.Namespace(disable=set(), no_soft=False, check_quoted=False,
                              max_steps=DEFAULT_MAX_STEP, max_desc=DEFAULT_MAX_DESC)
    failures = []

    found, _ = check(clean.read_text(encoding="utf-8"), rules, args)
    if found:
        failures.append(f"clean.md 应 0 命中，实际 {[(f['id'], f['line']) for f in found]}")

    text = bad.read_text(encoding="utf-8")
    lines = text.splitlines()
    marks = [(i + 1, m.group(1)) for i, l in enumerate(lines)
             if (m := re.search(r"<!--\s*expect:\s*([\w\-.]+)\s*-->", l))]
    found, _ = check(text, rules, args)
    for idx, (lineno, expect) in enumerate(marks):
        end = marks[idx + 1][0] - 1 if idx + 1 < len(marks) else len(lines)
        got = {f["id"] for f in found if lineno < f["line"] <= end}
        if expect == "none":
            ok = not got
        elif expect == "H1":
            ok = "H1" in got
        elif expect == "H2":
            ok = "H2" in got
        else:
            ok = got == {expect}
        if not ok:
            failures.append(f"bad.md:{lineno} 预期 {expect}，实际 {sorted(got) or '无'}")
        print(f"  {'ok  ' if ok else 'FAIL'} 行 {lineno} 预期 {expect}")

    for text, expect in INLINE_CASES:
        got = {f["id"] for f in check(text, rules, args)[0]}
        ok = got == expect
        if not ok:
            failures.append(f"内联用例预期 {sorted(expect) or '无'}，实际 {sorted(got) or '无'}：{text.splitlines()[0]}")
        print(f"  {'ok  ' if ok else 'FAIL'} 内联 {text.splitlines()[0][:24]}")

    if failures:
        for f in failures:
            print("FAIL", f, file=sys.stderr)
        return 1
    print("selftest OK")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="检查中文输出的结构、用词和句式。")
    ap.add_argument("paths", nargs="*", help="要检查的文件，- 表示 stdin")
    ap.add_argument("--text", help="直接给一段文字")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--disable", default="", help="逗号分隔的 id 或规则号，如 r29-01,R31")
    ap.add_argument("--no-soft", action="store_true", help="不报告 soft 项")
    ap.add_argument("--check-quoted", action="store_true", help="连引号内的内容一起检查")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEP)
    ap.add_argument("--max-desc", type=int, default=DEFAULT_MAX_DESC)
    ap.add_argument("--selftest", action="store_true", help="跑 tests/ 下的正反样本")
    args = ap.parse_args(argv)
    args.disable = {x.strip() for x in args.disable.split(",") if x.strip()}

    if args.selftest:
        return selftest(Path(__file__).resolve().parent.parent / "tests")

    try:
        rules = load_rules(WORDLIST)
    except DataError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.text is not None:
        sources = [("-", args.text)]
    elif args.paths:
        sources = []
        for p in args.paths:
            if p == "-":
                sources.append(("-", sys.stdin.read()))
            else:
                path = Path(p)
                if not path.exists():
                    print(f"找不到文件：{p}", file=sys.stderr)
                    return 2
                sources.append((p, path.read_text(encoding="utf-8")))
    else:
        ap.print_help(sys.stderr)
        return 2

    all_found, any_hard = [], False
    for name, text in sources:
        found, stats = check(text, rules, args)
        if stats["unclosed_fence"]:
            print(f"{name}:{stats['unclosed_fence']} 代码块没有闭合。"
                  f"从这一行到文件末尾都没检查，不当作通过。", file=sys.stderr)
            return 2
        all_found.extend(found)
        if args.json:
            continue
        any_hard = report(found, stats, name, args, sys.stdout) or any_hard

    if args.json:
        print(json.dumps(all_found, ensure_ascii=False, indent=2))
        any_hard = any(f["severity"] == "hard" for f in all_found)
    return 1 if any_hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
