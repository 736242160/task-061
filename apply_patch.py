#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_patch.py — 纯 Python 标准库的统一 diff（unified diff）补丁应用工具

功能：
  * 解析统一 diff 补丁：文件头 ---/+++、块头 @@ -a,b +c,d @@、上下文/删除/新增行
  * 先按块头声明的行号定位；对不上时在 ±N 行窗口内按“上下文+删除行”精确匹配
    （N 由 --max-offset 指定，默认 10，理由见文末说明）
  * 某个块完全找不到时，报告是第几个块、在哪一行附近失败；且整个补丁不应用
  * 校验块内删除/新增行数与块头声明的范围是否一致，不一致则报错
  * 全部块要么全部应用成功，要么报告所有失败块并退出，绝不只应用一部分
  * 补丁含多个文件段时，按目标文件名自动选择对应段

用法：
  python3 apply_patch.py 补丁文件 目标文件 [-o 输出文件] [--max-offset N]
  python3 apply_patch.py patch.diff file.txt            # 结果输出到标准输出
  python3 apply_patch.py patch.diff file.txt -o new.txt # 结果写入文件
  python3 apply_patch.py - file.txt < patch.diff        # 补丁从标准输入读

退出码：0 成功；1 失败（补丁格式错误或有块无法定位，不产生任何输出文件）

偏移量默认值说明：默认允许 ±10 行偏移。补丁通常打在与其生成版本相近的文件上，
行号漂移一般只是上方增删了少量行；10 行足以覆盖常见的漂移，同时因为匹配时
要求“上下文行+删除行”逐字节完全一致，窗口再大也不会显著增加误匹配风险，
反而会让定位变慢、让错位补丁更难被发现，故取 10 为默认，可用 --max-offset 调整。
"""

import argparse
import os
import re
import sys

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Hunk:
    """一个补丁块：行号范围声明 + 旧文本镜像（上下文+删除行）+ 新文本镜像（上下文+新增行）。"""

    def __init__(self, index, old_start, old_count, new_start, new_count):
        self.index = index            # 在整个补丁中的块序号（从 1 开始）
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.old_lines = []           # 上下文行 + 删除行（保留换行符）
        self.new_lines = []           # 上下文行 + 新增行（保留换行符）

    def base_pos(self):
        # 旧文本在目标文件中的 0 基起始下标；纯插入块（old_count=0）插在 old_start 行之后
        return self.old_start - 1 if self.old_count > 0 else self.old_start


class Section:
    """补丁中一个文件段（---/+++ 头部 + 若干块）。"""

    def __init__(self, old_name, new_name):
        self.old_name = old_name
        self.new_name = new_name
        self.hunks = []


def parse_patch(patch_text):
    """解析补丁文本，返回 (sections, errors)。errors 为块行数与声明不符等格式错误。"""
    lines = patch_text.split("\n")
    sections = []
    errors = []
    hunk_counter = 0
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith("--- "):
            i += 1  # 跳过 diff --git、index 等无关行
            continue
        old_name = line[4:].split("\t")[0].strip()
        if i + 1 >= n or not lines[i + 1].startswith("+++ "):
            errors.append("发现 '---' 文件头但缺少对应的 '+++' 行（第 %d 行附近）" % (i + 1))
            i += 1
            continue
        new_name = lines[i + 1][4:].split("\t")[0].strip()
        section = Section(old_name, new_name)
        sections.append(section)
        i += 2
        while i < n:
            m = HUNK_RE.match(lines[i])
            if not m:
                break
            hunk_counter += 1
            hunk = Hunk(
                hunk_counter,
                int(m.group(1)), int(m.group(2) or 1),
                int(m.group(3)), int(m.group(4) or 1),
            )
            i += 1
            last_lists = []
            while i < n:
                cur = lines[i]
                if cur.startswith("\\"):
                    # "\ No newline at end of file"：前一行不带换行符
                    for lst in last_lists:
                        if lst and lst[-1].endswith("\n"):
                            lst[-1] = lst[-1][:-1]
                    i += 1
                    continue
                old_done = len(hunk.old_lines) >= hunk.old_count
                new_done = len(hunk.new_lines) >= hunk.new_count
                if old_done and new_done:
                    break
                if cur == "":
                    tag, content = " ", "\n"  # 兼容省略前导空白的空上下文行
                elif cur[0] in " -+":
                    tag, content = cur[0], cur[1:] + "\n"
                else:
                    break  # 块提前结束，行数校验会在下面报错
                if tag in " -":
                    hunk.old_lines.append(content)
                if tag in " +":
                    hunk.new_lines.append(content)
                last_lists = (
                    [hunk.old_lines, hunk.new_lines] if tag == " "
                    else [hunk.old_lines] if tag == "-"
                    else [hunk.new_lines]
                )
                i += 1
            if len(hunk.old_lines) != hunk.old_count or len(hunk.new_lines) != hunk.new_count:
                errors.append(
                    "块 #%d：行数与块头声明不符（声明 -%d,%d +%d,%d，实际删除+上下文 %d 行、"
                    "新增+上下文 %d 行）" % (
                        hunk.index, hunk.old_start, hunk.old_count,
                        hunk.new_start, hunk.new_count,
                        len(hunk.old_lines), len(hunk.new_lines),
                    )
                )
            section.hunks.append(hunk)
    return sections, errors


def normalize_name(name):
    """去掉 a/ b/ 前缀；/dev/null 返回 None。"""
    if name == "/dev/null":
        return None
    for prefix in ("a/", "b/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def select_section(sections, target_path):
    """补丁含多个文件段时，按目标文件名选择；只有一段时直接使用。"""
    if len(sections) == 1:
        return sections[0]
    base = os.path.basename(target_path)
    for section in sections:
        for name in (normalize_name(section.new_name), normalize_name(section.old_name)):
            if name and os.path.basename(name) == base:
                return section
    names = ", ".join(
        normalize_name(s.new_name) or normalize_name(s.old_name) or "?" for s in sections
    )
    raise SystemExit("错误：补丁包含多个文件段（%s），没有与目标文件 '%s' 匹配的段" % (names, base))


def match_at(file_lines, pos, old_lines):
    if pos < 0 or pos + len(old_lines) > len(file_lines):
        return False
    return file_lines[pos:pos + len(old_lines)] == old_lines


def locate_hunk(file_lines, hunk, max_offset):
    """在声明行号 ±max_offset 范围内寻找精确匹配，返回起始下标或 None。"""
    base = hunk.base_pos()
    for dist in range(max_offset + 1):
        candidates = (base,) if dist == 0 else (base - dist, base + dist)
        for pos in candidates:
            if match_at(file_lines, pos, hunk.old_lines):
                return pos
    return None


def preview(line, limit=60):
    text = line.rstrip("\n")
    return text if len(text) <= limit else text[:limit] + "..."


def apply_patch(target_text, section, max_offset):
    """定位并应用所有块。成功返回 (新文本, 定位信息)；失败返回 (None, 失败列表)。"""
    file_lines = target_text.splitlines(keepends=True)
    located, failures = [], []
    for hunk in section.hunks:
        pos = locate_hunk(file_lines, hunk, max_offset)
        if pos is None:
            failures.append(hunk)
        else:
            located.append((hunk, pos))
    if failures:
        return None, failures

    located.sort(key=lambda hp: hp[1])
    for (h1, p1), (h2, p2) in zip(located, located[1:]):
        if p1 + len(h1.old_lines) > p2:
            raise SystemExit(
                "错误：块 #%d 与块 #%d 的源区域重叠，无法安全应用" % (h1.index, h2.index)
            )

    result = list(file_lines)
    for hunk, pos in sorted(located, key=lambda hp: hp[1], reverse=True):  # 从后往前替换，互不影响
        result[pos:pos + len(hunk.old_lines)] = hunk.new_lines
    return "".join(result), located


def read_text(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把统一 diff 补丁应用到目标文件（全部成功才输出，否则报告失败块）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("patch", help="补丁文件路径，'-' 表示从标准输入读取")
    parser.add_argument("target", help="目标文件路径")
    parser.add_argument("-o", "--output", help="输出文件路径，默认输出到标准输出")
    parser.add_argument("--max-offset", type=int, default=10,
                        help="行号对不上时允许上下搜索的最大偏移行数，默认 10")
    args = parser.parse_args(argv)

    patch_text = read_text(args.patch)
    sections, errors = parse_patch(patch_text)
    if errors:
        for msg in errors:
            print("补丁格式错误：" + msg, file=sys.stderr)
        return 1
    if not sections:
        print("补丁格式错误：未找到任何文件头（---/+++）", file=sys.stderr)
        return 1

    section = select_section(sections, args.target)
    if not section.hunks:
        print("补丁格式错误：文件段中没有任何补丁块（@@）", file=sys.stderr)
        return 1

    target_text = read_text(args.target)
    new_text, info = apply_patch(target_text, section, args.max_offset)

    if new_text is None:
        print("错误：%d 个块无法定位，补丁未应用（未产生任何输出）：" % len(info), file=sys.stderr)
        for hunk in info:
            where = hunk.old_start if hunk.old_count > 0 else hunk.old_start + 1
            print("  块 #%d：在第 %d 行附近（±%d 行内）找不到匹配" % (hunk.index, where, args.max_offset),
                  file=sys.stderr)
            if hunk.old_lines:
                print("    期望匹配的首行：%s" % preview(hunk.old_lines[0]), file=sys.stderr)
        return 1

    for hunk, pos in info:
        expect = hunk.base_pos()
        shift = pos - expect
        note = "行号精确命中" if shift == 0 else "按上下文匹配，偏移 %+d 行" % shift
        print("块 #%d：已应用（%s，落在第 %d 行）" % (hunk.index, note, pos + 1), file=sys.stderr)

    if args.output:
        with open(args.output, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(new_text)
        print("已写入：%s" % args.output, file=sys.stderr)
    else:
        sys.stdout.write(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
