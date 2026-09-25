#!/usr/bin/env python3
"""apply_patch.py — 纯 Python 标准库的统一 diff（unified diff）补丁应用工具。

用法：
    python3 apply_patch.py PATCH TARGET [-o OUTPUT] [--max-offset N]
    python3 apply_patch.py patch.diff target.txt              # 结果写标准输出
    python3 apply_patch.py patch.diff target.txt -o out.txt   # 结果写文件
    python3 apply_patch.py patch.diff target.txt --in-place   # 就地改写目标文件
    python3 apply_patch.py - target.txt < patch.diff          # 补丁从标准输入读

行为约定：
  * 定位：先按块头 @@ -old_start,old_count @@ 声明的行号定位；对不上时，
    在 ±N 行窗口内用“上下文行 + 删除行”做精确匹配（默认 N=10，
    可用 --max-offset 调整）。默认 10 行的理由：补丁生成之后目标文件
    通常只被小幅改动（增删若干行），漂移量很小；而窗口过大时，文件中
    重复的样板内容可能造成误匹配。10 行是“容忍常见漂移”与“避免远处
    误配”之间的经验平衡点。本工具只做精确匹配、不做模糊（fuzz）匹配，
    宁可报失败也不打错位置。
  * 原子性：所有块必须全部定位成功才写出结果；任何一块失败都会报告
    块号、声明的行号附近位置与期望内容预览，并且不修改任何文件。
  * 行数校验：块内“上下文+删除”行数必须等于 - 侧声明行数，
    “上下文+新增”行数必须等于 + 侧声明行数，否则报告块号与差异，
    拒绝应用。
  * 多文件补丁：自动按目标文件名选择对应的文件段。

退出码：0 成功；1 有块无法定位 / 行数不符 / 块间重叠；2 解析或参数错误。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s?(.*)$")

GIT_HEADER_PREFIXES = (
    "diff --git ", "index ", "new file mode ", "deleted file mode ",
    "old mode ", "new mode ", "similarity index ", "dissimilarity index ",
    "rename from ", "rename to ", "copy from ", "copy to ",
    "Binary files ", "GIT binary patch",
)


class PatchParseError(Exception):
    pass


@dataclass
class Hunk:
    number: int          # 全局块号（从 1 开始）
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list = field(default_factory=list)  # (kind, text)，kind ∈ {' ', '-', '+'}
    no_newline_old: bool = False
    no_newline_new: bool = False

    def old_side(self):
        return [text for kind, text in self.lines if kind != "+"]

    def new_side(self):
        return [text for kind, text in self.lines if kind != "-"]

    def header(self):
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"


@dataclass
class FilePatch:
    old_path: str = ""
    new_path: str = ""
    hunks: list = field(default_factory=list)


def _strip_path(raw):
    path = raw.split("\t", 1)[0].strip()
    if len(path) > 2 and path[0] in "ab" and path[1] == "/":
        path = path[2:]
    return path


def _count_lines(hunk):
    old = sum(1 for kind, _ in hunk.lines if kind in (" ", "-"))
    new = sum(1 for kind, _ in hunk.lines if kind in (" ", "+"))
    return old, new


def parse_patch(text):
    files = []
    current_file = None
    current_hunk = None
    hunk_counter = 0

    def ensure_file():
        nonlocal current_file
        if current_file is None:
            current_file = FilePatch()
            files.append(current_file)
        return current_file

    patch_lines = text.split("\n")
    if patch_lines and patch_lines[-1] == "":
        patch_lines.pop()  # 去掉文本末尾换行产生的空串，避免被误认为空上下文行
    for lineno, raw in enumerate(patch_lines, 1):
        if raw.startswith(GIT_HEADER_PREFIXES):
            continue
        if raw.startswith("--- "):
            current_file = FilePatch(old_path=_strip_path(raw[4:]))
            files.append(current_file)
            current_hunk = None
            continue
        if raw.startswith("+++ "):
            ensure_file().new_path = _strip_path(raw[4:])
            continue
        m = HUNK_HEADER_RE.match(raw)
        if m:
            hunk_counter += 1
            current_hunk = Hunk(
                number=hunk_counter,
                old_start=int(m.group(1)),
                old_count=int(m.group(2) or "1"),
                new_start=int(m.group(3)),
                new_count=int(m.group(4) or "1"),
                section=m.group(5) or "",
            )
            ensure_file().hunks.append(current_hunk)
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            if current_hunk is not None and current_hunk.lines:
                kind = current_hunk.lines[-1][0]
                if kind in (" ", "-"):
                    current_hunk.no_newline_old = True
                if kind in (" ", "+"):
                    current_hunk.no_newline_new = True
            continue
        if current_hunk is not None:
            old_seen, new_seen = _count_lines(current_hunk)
            complete = old_seen >= current_hunk.old_count and new_seen >= current_hunk.new_count
            if raw[:1] in (" ", "-", "+"):
                current_hunk.lines.append((raw[0], raw[1:]))
                continue
            if raw == "" and not complete:
                # 有些工具把内容为空的上下文行输出成裸空行，宽容处理
                current_hunk.lines.append((" ", ""))
                continue
            if complete:
                current_hunk = None  # 块已满足声明行数，本行按块外杂散行忽略
                continue
            raise PatchParseError(
                f"第 {lineno} 行：第 {current_hunk.number} 个块内出现无法识别的行：{raw!r}"
            )
        # 块外杂散行（说明文字等）忽略
    return files


def validate_counts(files):
    errors = []
    for fp in files:
        for hunk in fp.hunks:
            old_seen, new_seen = _count_lines(hunk)
            if old_seen != hunk.old_count or new_seen != hunk.new_count:
                errors.append(
                    f"第 {hunk.number} 个块（{hunk.header()}）行数与声明不符：\n"
                    f"  删除侧（上下文+删除）声明 {hunk.old_count} 行，实际 {old_seen} 行；\n"
                    f"  新增侧（上下文+新增）声明 {hunk.new_count} 行，实际 {new_seen} 行。"
                )
    return errors


def _candidate_offsets(max_offset):
    yield 0
    for d in range(1, max_offset + 1):
        yield d
        yield -d


def locate_hunk(lines, hunk, max_offset):
    """返回块在原文件中的 0 基起始行号；找不到返回 None。"""
    if hunk.old_count == 0:
        # 纯新增块：没有可匹配内容，插入点直接由行号确定
        # （-N,0 表示在原文件第 N 行之后插入，即 0 基下标 N 处）
        pos = hunk.old_start
        return pos if 0 <= pos <= len(lines) else None
    expected = hunk.old_start - 1
    old_side = hunk.old_side()
    n = len(old_side)
    for offset in _candidate_offsets(max_offset):
        pos = expected + offset
        if 0 <= pos and pos + n <= len(lines) and lines[pos:pos + n] == old_side:
            return pos
    return None


def apply_hunks(lines, hunks, max_offset):
    """全部块定位成功才返回新行序列；否则返回失败列表，不产生任何部分结果。

    返回 (new_lines, failures, placements)。failures 为 [(hunk, reason), ...]。
    """
    failures = []
    placements = []
    for hunk in hunks:
        pos = locate_hunk(lines, hunk, max_offset)
        if pos is None:
            failures.append((hunk, "not-found"))
        else:
            placements.append((pos, hunk))
    if failures:
        return None, failures, []

    placements.sort(key=lambda item: item[0])
    for (prev_pos, prev_hunk), (pos, hunk) in zip(placements, placements[1:]):
        if pos < prev_pos + prev_hunk.old_count:
            failures.append((hunk, f"overlap:{prev_hunk.number}"))
    if failures:
        return None, failures, []

    out = []
    cursor = 0
    for pos, hunk in placements:
        out.extend(lines[cursor:pos])
        out.extend(hunk.new_side())
        cursor = pos + hunk.old_count
    out.extend(lines[cursor:])
    return out, [], placements


def split_text(text):
    if text == "":
        return [], True
    trailing = text.endswith("\n")
    body = text[:-1] if trailing else text
    return body.split("\n"), trailing


def resolve_trailing_newline(trailing, num_lines, placements):
    """根据触及文件末尾的块所携带的换行标记决定输出是否以换行结尾。

    只有补丁明确带有 "\\ No newline at end of file" 标记时才改变原文件的
    结尾状态；没有标记时（例如 difflib 生成的补丁）保持原样。
    """
    if not placements:
        return trailing
    last_pos, last_hunk = max(placements, key=lambda item: item[0])
    if last_pos + last_hunk.old_count != num_lines:
        return trailing
    if last_hunk.no_newline_old or last_hunk.no_newline_new:
        return not last_hunk.no_newline_new
    return trailing


def _select_file_patch(file_patches, target):
    with_hunks = [fp for fp in file_patches if fp.hunks]
    if len(with_hunks) == 1:
        return with_hunks[0]
    if not with_hunks:
        return None
    base = os.path.basename(target)
    for fp in with_hunks:
        if fp.new_path and os.path.basename(fp.new_path) == base:
            return fp
    for fp in with_hunks:
        if fp.old_path and os.path.basename(fp.old_path) == base:
            return fp
    return None


def _report_failures(failures, total, max_offset):
    for hunk, reason in failures:
        if reason == "not-found":
            print(
                f"错误：第 {hunk.number} 个块无法定位（{hunk.header()}，"
                f"期望在原文件第 {hunk.old_start} 行附近，允许偏移 ±{max_offset} 行）。",
                file=sys.stderr,
            )
            preview = hunk.lines[:4]
            print(f"  期望在该处匹配到以下内容（共 {len(hunk.lines)} 行，预览前 {len(preview)} 行）：",
                  file=sys.stderr)
            for kind, text in preview:
                print(f"  {kind}{text}", file=sys.stderr)
            print("  提示：目标文件可能已被改动，或补丁与文件版本不对应。", file=sys.stderr)
        elif reason.startswith("overlap:"):
            print(
                f"错误：第 {hunk.number} 个块与第 {reason.split(':', 1)[1]} 个块的应用区域重叠，"
                f"补丁本身可能有误。",
                file=sys.stderr,
            )
    print(f"共 {total} 个块，{len(failures)} 个失败；按“全部或全不”原则，未写出任何修改。",
          file=sys.stderr)


def _read(path, encoding):
    with open(path, "r", encoding=encoding, newline="") as f:
        return f.read()


def _write(path, text, encoding):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="apply_patch.py",
        description="把统一 diff 补丁应用到目标文件。所有块全部成功才写出，否则报告失败块。",
        epilog="定位策略：先按块头行号定位，失败时在 ±N 行窗口内按上下文/删除行精确匹配"
               "（默认 N=10，--max-offset 可调）。",
    )
    parser.add_argument("patch", help="补丁文件路径，'-' 表示从标准输入读取")
    parser.add_argument("target", help="要打补丁的目标文件")
    parser.add_argument("-o", "--output", help="输出文件（默认写标准输出）")
    parser.add_argument("--in-place", action="store_true", help="直接改写目标文件")
    parser.add_argument("--max-offset", type=int, default=10, metavar="N",
                        help="行号对不上时允许搜索的偏移窗口，默认 10 行")
    parser.add_argument("--encoding", default="utf-8", help="文件编码，默认 utf-8")
    args = parser.parse_args(argv)

    if args.in_place and args.output:
        parser.error("--in-place 与 -o/--output 不能同时使用")
    if args.max_offset < 0:
        parser.error("--max-offset 不能为负数")

    try:
        patch_text = sys.stdin.read() if args.patch == "-" else _read(args.patch, args.encoding)
    except OSError as exc:
        print(f"错误：无法读取补丁文件：{exc}", file=sys.stderr)
        return 2
    try:
        target_text = _read(args.target, args.encoding)
    except OSError as exc:
        print(f"错误：无法读取目标文件：{exc}", file=sys.stderr)
        return 2

    try:
        file_patches = parse_patch(patch_text)
    except PatchParseError as exc:
        print(f"错误：补丁解析失败：{exc}", file=sys.stderr)
        return 2

    count_errors = validate_counts(file_patches)
    if count_errors:
        for err in count_errors:
            print(f"错误：{err}", file=sys.stderr)
        print("补丁未应用（全部或全不）。", file=sys.stderr)
        return 1

    file_patch = _select_file_patch(file_patches, args.target)
    if file_patch is None:
        available = [fp.new_path or fp.old_path or "?" for fp in file_patches if fp.hunks]
        print(f"错误：补丁中没有与目标文件 {args.target!r} 对应的文件段"
              f"（补丁包含：{', '.join(available) or '无'}）。", file=sys.stderr)
        return 2

    lines, trailing_newline = split_text(target_text)
    new_lines, failures, placements = apply_hunks(lines, file_patch.hunks, args.max_offset)
    if failures:
        _report_failures(failures, len(file_patch.hunks), args.max_offset)
        return 1

    trailing = resolve_trailing_newline(trailing_newline, len(lines), placements)
    result = "\n".join(new_lines)
    if trailing and new_lines:
        result += "\n"

    ok_msg = f"{len(file_patch.hunks)} 个块全部应用成功"
    if args.in_place:
        _write(args.target, result, args.encoding)
        print(f"已就地更新 {args.target}（{ok_msg}）。", file=sys.stderr)
    elif args.output:
        _write(args.output, result, args.encoding)
        print(f"已写出 {args.output}（{ok_msg}）。", file=sys.stderr)
    else:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
