#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
递归扫描目录及所有子目录中的 .ttf 文件，读取内部「出版方」与简繁字符倾向，
整理到根目录下的子文件夹，并将文件重命名为「字体名称.ttf」。

- 出版方：根据 Manufacturer、字族名、PostScript 等 name 表字段匹配「通俗名称」
  （如 方正、苍耳、华文、慧文）；规则表可在脚本内 _PUBLISHER_RULES 扩展。
  无法匹配但有厂商英文名时做简单去后缀；仍无法得到时，用**文件名与父目录名**
  再走同一套别名规则；最后才是「未标注出版方」。
- 简繁：根据 cmap 中简繁异形字覆盖与 name 表 Windows 语言 ID 推断；
  子文件夹为：简体、繁体、简繁通用、非中文、未分类。

最终路径：根目录 / 出版方文件夹 / 简繁文件夹 / 字体名称.ttf

默认处理脚本所在目录；也可传入目标目录路径。

依赖: pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("请先安装依赖: pip install fonttools", file=sys.stderr)
    sys.exit(1)

# Windows 及通用文件系统不安全字符
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# name 表 Windows 语言 ID：简体地区 vs 繁体地区（见 MSFT language identifiers）
_WIN_LANG_SIMPLIFIED = frozenset({0x0804, 0x1004})  # 中国大陆、新加坡（简体）
_WIN_LANG_TRADITIONAL = frozenset({0x0404, 0x0C04, 0x1404})  # 台湾、香港、澳门

# 越靠前的规则优先匹配（正则作用于合并后的 name 表文本）；需要忽略英文大小时整体加 re.I
_PUBLISHER_RULES: list[tuple[re.Pattern[str], str]] = [
    # 用户举例与常见大陆字库（可自行增补）
    (re.compile(r"慧文|滙文|匯文|汇文|huiwen", re.I), "慧文"),
    (
        re.compile(
            r"方正|founder|北大方正|beijing\s*[^,\n]*founder|foundertype",
            re.I,
        ),
        "方正",
    ),
    (re.compile(r"\bFZ[A-Z][A-Za-z0-9_-]+\b"), "方正"),
    (re.compile(r"苍耳|canger|tsanger", re.I), "苍耳"),
    (
        re.compile(
            r"华文|華文|huawen|"
            r"STHeiti|STSong|STKaiti|STFangsong|STXinwei|STXingkai|"
            r"STCaiyun|STZhongsong|STXihei|STYuanti",
            re.I,
        ),
        "华文",
    ),
    (re.compile(r"京华|kinghwa", re.I), "京华"),
    (
        re.compile(
            r"微信读书|\bweread\b|wechat\s*reading",
            re.I,
        ),
        "微信读书",
    ),
    (re.compile(r"汉仪|hanyi|\bHY[A-Z][A-Za-z0-9_-]*\b", re.I), "汉仪"),
    (
        re.compile(r"思源|source\s*han|sourcehan|noto\s+(sans|serif)\s+cjk", re.I),
        "思源",
    ),
    (re.compile(r"字魂|zihun", re.I), "字魂"),
    (re.compile(r"造字工房|makefont", re.I), "造字工房"),
    (re.compile(r"阿里妈妈|阿里巴巴|alibaba|alimama", re.I), "阿里"),
    (re.compile(r"\badobe\b", re.I), "Adobe"),
    (re.compile(r"\bgoogle\b", re.I), "Google"),
    (re.compile(r"华康|dyna(?:font|com|comware)?", re.I), "华康"),
    (re.compile(r"microsoft|微软", re.I), "微软"),
]

_CORP_SUFFIX_RE = re.compile(
    r"(?i)[,，\s]*("
    r"(co\.,?\s*)?ltd\.?|limited(\s+company)?|inc\.?|llc|gmbh|"
    r"corp\.?|corporation|group|公司|股份有限公司|电子有限公司"
    r")\.?$"
)

# (简体码点, 繁体码点) 常用同义异形字，用于从 cmap 推断简繁覆盖
_SC_TC_MARKER_PAIRS: list[tuple[int, int]] = [
    (0x56FD, 0x570B),  # 国 國
    (0x5B66, 0x5B78),  # 学 學
    (0x6C49, 0x6F22),  # 汉 漢
    (0x4F53, 0x9AD4),  # 体 體
    (0x7535, 0x96FB),  # 电 電
    (0x9F99, 0x9F8D),  # 龙 龍
    (0x95E8, 0x9580),  # 门 門
    (0x89C1, 0x898B),  # 见 見
    (0x957F, 0x9577),  # 长 長
    (0x4E1C, 0x6771),  # 东 東
    (0x8F66, 0x8ECA),  # 车 車
    (0x8D1D, 0x8C9D),  # 贝 貝
    (0x4E49, 0x7FA9),  # 义 義
    (0x5934, 0x982D),  # 头 頭
    (0x4E13, 0x5C08),  # 专 專
]


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = INVALID_CHARS.sub("_", name.strip())
    name = re.sub(r"\s+", " ", name).strip()
    # Windows 保留名
    stem = Path(name).stem if "." in name else name
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.match(
        r"^(COM|LPT)\d$", stem.upper()
    ):
        name = f"_{name}"
    if len(name) > max_len:
        root, ext = (name.rsplit(".", 1) + [""])[:2]
        if ext.lower() == "ttf":
            name = root[: max_len - 4] + ".ttf"
        else:
            name = name[:max_len]
    return name or "unnamed_font"


def sanitize_dirname(name: str, max_len: int = 120) -> str:
    """文件夹名：去掉 Windows 不允许的尾点、尾空格等。"""
    s = INVALID_CHARS.sub("_", name.strip())
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    stem = Path(s).stem if "." in s and not s.endswith(".") else s
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.match(
        r"^(COM|LPT)\d$", stem.upper()
    ):
        s = f"_{s}"
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s or "未标注出版方"


def pick_publisher(tt: TTFont) -> str | None:
    """
    出版方 / 厂商：OpenType nameID 8「Manufacturer Name」。
    同名字段内优先 Windows（platform 3），其次 Mac（platform 1）。
    """
    tab = tt.get("name")
    if not tab or not tab.names:
        return None

    def plat_key(rec):
        return 0 if rec.platformID == 3 else 1 if rec.platformID == 1 else 2

    for rec in sorted((r for r in tab.names if r.nameID == 8), key=plat_key):
        try:
            s = rec.toUnicode()
        except Exception:
            continue
        if s and s.strip():
            return s.strip()
    return None


def _iter_name_strings(tt: TTFont, name_ids: frozenset[int]) -> list[str]:
    tab = tt.get("name")
    if not tab or not tab.names:
        return []
    out: list[str] = []
    for rec in tab.names:
        if rec.nameID not in name_ids:
            continue
        try:
            s = rec.toUnicode()
        except Exception:
            continue
        if s and s.strip():
            out.append(s.strip())
    return out


def _publisher_match_haystack(tt: TTFont) -> str:
    """合并若干 name 字段，供出版方别名匹配。"""
    ids = frozenset({0, 1, 3, 4, 6, 8, 9})
    parts = _iter_name_strings(tt, ids)
    return "\n".join(parts)


def _humanize_manufacturer(raw: str) -> str:
    """未命中别名时，去掉常见公司后缀，便于作文件夹名。"""
    s = raw.strip()
    prev = None
    while prev != s:
        prev = s
        s = _CORP_SUFFIX_RE.sub("", s).strip(" ,，.。．")
    return s.strip() or raw.strip()


def _path_publisher_haystack(path: Path) -> str:
    """从文件主名、按分隔符切分片段、以及父目录名拼成文本，供出版方规则匹配。"""
    seen: set[str] = set()
    parts: list[str] = []

    def add(s: str) -> None:
        t = s.strip()
        if not t or t in seen:
            return
        seen.add(t)
        parts.append(t)

    for raw in (path.stem, path.parent.name):
        if not raw:
            continue
        add(raw)
        for seg in re.split(r"[-_+.]+", raw):
            add(seg)
    return "\n".join(parts)


def _publisher_from_path(path: Path) -> str | None:
    """对路径文本套用与元数据相同的 _PUBLISHER_RULES。"""
    hay = _path_publisher_haystack(path)
    if not hay.strip():
        return None
    for pat, label in _PUBLISHER_RULES:
        if pat.search(hay):
            return label
    return None


def resolve_publisher_common_name(
    tt: TTFont, source_path: Path | None = None
) -> str:
    """
    解析用于目录的「通俗出版方」：先 name 表 + _PUBLISHER_RULES，否则 Manufacturer 简写，
    仍为未标注时再用 source_path（文件名与父目录名）走同一套规则。
    """
    haystack = _publisher_match_haystack(tt)
    for pat, label in _PUBLISHER_RULES:
        if haystack and pat.search(haystack):
            return label
    man = pick_publisher(tt)
    if man:
        short = _humanize_manufacturer(man)
        if short:
            return short
    if source_path is not None:
        from_path = _publisher_from_path(source_path)
        if from_path:
            return from_path
    return "未标注出版方"


def _unicode_cmap(tt: TTFont) -> dict | None:
    cm = tt.get("cmap")
    if not cm:
        return None
    return cm.getBestCmap()


def _cmap_has_char(cmap: dict, cp: int) -> bool:
    g = cmap.get(cp)
    return g is not None and g != ".notdef"


def _windows_name_lang_ids(tt: TTFont) -> set[int]:
    ids: set[int] = set()
    tab = tt.get("name")
    if not tab:
        return ids
    for rec in tab.names:
        if rec.platformID != 3:
            continue
        ids.add(rec.langID & 0xFFFF)
    return ids


def classify_han_variant(tt: TTFont) -> str:
    """
    根据 cmap 简繁异形字对与 name 表语言 ID，返回用于子文件夹的标签。
    """
    cmap = _unicode_cmap(tt)
    if not cmap:
        return "未分类"

    has_cjk = any(0x4E00 <= cp <= 0x9FFF for cp in cmap)
    if not has_cjk:
        return "非中文"

    both = sc_only = tc_only = 0
    for sc_cp, tc_cp in _SC_TC_MARKER_PAIRS:
        h_sc = _cmap_has_char(cmap, sc_cp)
        h_tc = _cmap_has_char(cmap, tc_cp)
        if h_sc and h_tc:
            both += 1
        elif h_sc:
            sc_only += 1
        elif h_tc:
            tc_only += 1

    marked = both + sc_only + tc_only
    langs = _windows_name_lang_ids(tt)
    simp_hit = bool(langs & _WIN_LANG_SIMPLIFIED)
    trad_hit = bool(langs & _WIN_LANG_TRADITIONAL)

    # 同时覆盖多对简繁字形 → 泛 CJK / 通用字体
    if both >= 5:
        return "简繁通用"
    if both >= 3 and sc_only <= 1 and tc_only <= 1:
        return "简繁通用"

    if sc_only - tc_only >= 3:
        return "简体"
    if tc_only - sc_only >= 3:
        return "繁体"
    if marked >= 3:
        if sc_only > tc_only:
            return "简体"
        if tc_only > sc_only:
            return "繁体"

    if marked < 3:
        if simp_hit and not trad_hit:
            return "简体"
        if trad_hit and not simp_hit:
            return "繁体"
        if simp_hit and trad_hit:
            return "简繁通用"
        if sc_only > tc_only:
            return "简体"
        if tc_only > sc_only:
            return "繁体"
        return "未分类"

    if both > 0:
        return "简繁通用"
    if simp_hit and not trad_hit:
        return "简体"
    if trad_hit and not simp_hit:
        return "繁体"
    return "未分类"


def pick_preferred_name(tt: TTFont) -> str | None:
    """
    优先 Full font name (4)，其次 Family (1)，最后 PostScript name (6)。
    优先 platform 3 (Windows) / 1 (Mac) 的 Unicode / UTF-16 / MacRoman。
    """
    name = tt.get("name")
    if not name or not name.names:
        return None

    def key(rec):
        # 排序：nameID 优先级 4 < 1 < 6；platform 3 优先；lang 中文等可后考虑，这里简单按 platform
        nid_order = {4: 0, 1: 1, 6: 2}.get(rec.nameID, 99)
        plat = 0 if rec.platformID == 3 else 1 if rec.platformID == 1 else 2
        return (nid_order, plat)

    sorted_recs = sorted(name.names, key=key)
    for rec in sorted_recs:
        if rec.nameID not in (1, 4, 6):
            continue
        try:
            s = rec.toUnicode()
        except Exception:
            continue
        if s and s.strip():
            return s.strip()
    return None


def unique_path(
    target: Path,
    used: set[Path],
    *,
    same_as: Path | None = None,
) -> Path:
    """same_as：若 target 已存在但即为当前源文件，仍视为可用（无需改成 _2）。"""

    def available(p: Path) -> bool:
        if p in used:
            return False
        if not p.exists():
            return True
        if same_as is not None and p.resolve() == same_as.resolve():
            return True
        return False

    if available(target):
        used.add(target)
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{suffix}")
        if available(candidate):
            used.add(candidate)
            return candidate
        n += 1


def rename_fonts(directory: Path, dry_run: bool) -> None:
    directory = directory.resolve()
    if not directory.is_dir():
        print(f"不是有效目录: {directory}", file=sys.stderr)
        sys.exit(1)

    ttf_files = sorted(directory.rglob("*.ttf"))
    if not ttf_files:
        print(f"未找到 .ttf 文件: {directory}")
        return

    # 按「出版方 + 简繁」目标目录分别避让重名
    used_targets: dict[Path, set[Path]] = defaultdict(set)

    def rel(p: Path) -> Path:
        try:
            return p.relative_to(directory)
        except ValueError:
            return p

    for path in ttf_files:
        path = path.resolve()
        try:
            with TTFont(path, fontNumber=0) as tt:
                display = pick_preferred_name(tt)
                pub_label = resolve_publisher_common_name(tt, path)
                variant_raw = classify_han_variant(tt)
        except Exception as e:
            print(f"[跳过] {rel(path)} 读取失败: {e}")
            continue

        if not display:
            print(f"[跳过] {rel(path)} 无法解析字体名称")
            continue

        publisher_folder = sanitize_dirname(pub_label)
        variant_folder = sanitize_dirname(variant_raw)
        dest_parent = (directory / publisher_folder / variant_folder).resolve()
        new_stem = sanitize_filename(display)
        bucket = used_targets[dest_parent]
        new_path = unique_path(
            dest_parent / f"{new_stem}.ttf", bucket, same_as=path
        )

        if path.resolve() == new_path.resolve():
            print(f"[不变] {rel(path)}")
            continue

        print(
            f"{'[预览] ' if dry_run else ''}{rel(path)} -> {rel(new_path)}"
        )
        if not dry_run:
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                safe_rename_tt(path, new_path)
            except OSError as e:
                print(f"[错误] 重命名/移动失败 {rel(path)}: {e}")
                bucket.discard(new_path)


def safe_rename_tt(src: Path, dst: Path) -> None:
    """
    重命名或跨目录移动。同目录且仅大小写不同时，在 Windows 上需经临时文件名。
    """
    src_r, dst_r = src.resolve(), dst.resolve()
    if src_r == dst_r:
        return
    same_dir = src_r.parent == dst_r.parent
    if same_dir and src.name.lower() == dst.name.lower():
        tmp = src.with_name(f"{src.stem}.__tmp_rename__{src.suffix}")
        src.rename(tmp)
        tmp.rename(dst)
    else:
        src.rename(dst)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按出版方与简繁分子目录整理，并按字体内部名称重命名 TTF"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="含 TTF 的目录（默认：脚本所在目录）",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="仅打印将要执行的改名，不写盘",
    )
    args = parser.parse_args()
    base = Path(args.directory) if args.directory else Path(__file__).resolve().parent
    rename_fonts(base, args.dry_run)


if __name__ == "__main__":
    main()
