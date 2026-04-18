#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
遍历仓库内所有 .ttf 与 font_previews/*.png，生成 Markdown。

- 按**同一出版方**分组（路径首段为出版方目录），每组一个 `##` 标题。
- 组内用**表格**横向排布：第一行为字体名链接，第二行为预览图（与
  `render_font_previews_html.py` 的 PNG 命名一致：主文件名 `.png`，冲突为 `_2` 等）。
- 文首第一段列出**全部出版方**的目录链接，跳转到对应 `##` 小节（HTML 锚点）。
- 「点击下载」链接：有 `--raw-base` 时用 raw 直链，否则用相对仓库路径。

用法:
  python scripts/generate_fonts_readme.py
  python scripts/generate_fonts_readme.py --out FONTS.md --branch main
  python scripts/generate_fonts_readme.py --raw-base 'https://raw.githubusercontent.com/owner/repo/main'
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

# 与 scripts/render_font_previews_html.py 中逻辑保持一致
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_SKIP_DIR_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "ENV",
    "env",
    "__pycache__",
    ".eggs",
    "node_modules",
})


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = INVALID_CHARS.sub("_", name.strip())
    name = re.sub(r"\s+", " ", name).strip() or "font"
    if len(name) > max_len:
        root, ext = (name.rsplit(".", 1) + [""])[:2]
        if ext.lower() == "png":
            name = root[: max_len - 4] + ".png"
        else:
            name = name[:max_len]
    return name


def find_preview_png_path(
    font_path: Path, preview_by_name: dict[str, Path]
) -> Path | None:
    """
    与 render_font_previews_html.unique_preview_png_name 的命名规则一致地查找 PNG。
    """
    stem = font_path.stem
    first = sanitize_filename(f"{stem}.png")
    if first in preview_by_name:
        return preview_by_name[first]
    root = Path(first).stem
    for n in range(2, 500):
        cand = sanitize_filename(f"{root}_{n}.png")
        if cand in preview_by_name:
            return preview_by_name[cand]
    return None


def rglob_ttf(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.ttf"):
        if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in p.parts):
            continue
        out.append(p)
    return sorted(out, key=lambda x: str(x).lower())


def publisher_heading_key(font_path: Path, root: Path) -> str:
    """用于 ## 分组的出版方：相对根目录的第一级目录名；根上的字体为「根目录」。"""
    rel = font_path.resolve().relative_to(root)
    if len(rel.parts) >= 2:
        return rel.parts[0]
    return "根目录"


def publisher_anchor_id(pub: str) -> str:
    """文内锚点 id，与 `<a id="...">` 及 `[文字](#...)` 一致。"""
    t = INVALID_CHARS.sub("-", pub.strip())
    t = re.sub(r'["\'&]', "-", t)
    t = t.replace(" ", "-")
    t = re.sub(r"-+", "-", t).strip("-")
    if not t:
        t = "root"
    return "pub-" + t


def parse_github_owner_repo(remote: str) -> tuple[str, str] | None:
    remote = remote.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote, re.I)
    if not m:
        return None
    return m.group(1), m.group(2).removesuffix(".git")


def git_cmd(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        s = (r.stdout or "").strip()
        return s or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def infer_github_raw_base(repo_root: Path, branch_override: str | None) -> str | None:
    remote = git_cmd(repo_root, "remote", "get-url", "origin")
    if not remote:
        return None
    parsed = parse_github_owner_repo(remote)
    if not parsed:
        return None
    owner, name = parsed
    branch = (
        branch_override
        or git_cmd(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        or "main"
    )
    return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}"


def encode_repo_rel_path(rel: Path) -> str:
    rel_posix = rel.as_posix()
    return "/".join(urllib.parse.quote(seg, safe="") for seg in rel_posix.split("/"))


def markdown_asset_url(readme_out: Path, asset_file: Path) -> str:
    """README 中可用的相对路径（段已 URL 编码，含空格与中文）。"""
    rel = Path(
        asset_file.resolve().relative_to(readme_out.resolve().parent)
    ).as_posix()
    return "/".join(urllib.parse.quote(seg, safe="") for seg in rel.split("/"))


def md_table_cell(text: str) -> str:
    """避免破坏 GFM 表格的竖线与换行。"""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def font_link_cell(
    readme_out: Path,
    font_path: Path,
    root: Path,
    raw_base: str,
    display: str,
) -> str:
    rel_ttf = font_path.resolve().relative_to(root)
    if raw_base:
        url = f"{raw_base.rstrip('/')}/{encode_repo_rel_path(rel_ttf)}"
    else:
        url = markdown_asset_url(readme_out, font_path)
    safe_disp = md_table_cell(display)
    return f"[{safe_disp}]({url})"


def image_cell(img_url: str | None, alt: str) -> str:
    if not img_url:
        return md_table_cell("*（尚无预览图）*")
    safe_alt = md_table_cell(alt.replace('"', "'"))
    return f"![{safe_alt}]({img_url})"


def emit_font_table(
    lines: list[str],
    fonts_in_group: list[Path],
    *,
    readme_out: Path,
    root: Path,
    raw_base: str,
    preview_by_name: dict[str, Path],
    missing_preview: list[Path],
) -> None:
    """同一出版方下：一行链接、一行预览图。"""
    fonts_in_group = sorted(fonts_in_group, key=lambda p: p.name.lower())
    link_cells: list[str] = []
    img_cells: list[str] = []
    for fp in fonts_in_group:
        disp = fp.stem
        ppath = find_preview_png_path(fp, preview_by_name)
        link_cells.append(font_link_cell(readme_out, fp, root, raw_base, disp))
        if ppath:
            alt = fp.stem.replace('"', "'").replace("]", "").replace("[", "")
            img_url = markdown_asset_url(readme_out, ppath)
            img_cells.append(image_cell(img_url, alt))
        else:
            missing_preview.append(fp)
            img_cells.append(image_cell(None, ""))

    n = len(link_cells)
    lines.append("| " + " | ".join(link_cells) + " |")
    lines.append("| " + " | ".join(["---"] * n) + " |")
    lines.append("| " + " | ".join(img_cells) + " |")
    lines.append("")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="生成 README：按出版方分组表格排布预览与下载链接"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo_root / "README.md",
        help="输出 Markdown 路径（默认：仓库根目录 README.md）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root,
        help="扫描字体的根目录（默认：仓库根目录）",
    )
    parser.add_argument(
        "--previews",
        type=Path,
        default=repo_root / "font_previews",
        help="预览 PNG 目录（默认：font_previews）",
    )
    parser.add_argument(
        "--raw-base",
        type=str,
        default=None,
        help="Raw 文件 URL 前缀，无尾斜杠，例如 https://raw.githubusercontent.com/o/r/main",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default=None,
        help="与 --raw-base 二选一：仅提供分支名时与 git remote 拼成 raw 地址（默认取当前分支）",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    prev_dir = args.previews.resolve()
    out_path = args.out.resolve()

    raw_base = (args.raw_base or "").rstrip("/")
    if not raw_base:
        raw_base = infer_github_raw_base(repo_root, args.branch) or ""
    if not raw_base:
        print(
            "提示: 未解析到 raw 基址，README 仍将生成本地/仓库相对路径图片；"
            "若需要 raw 直链可加 --raw-base。",
            file=sys.stderr,
        )

    fonts = rglob_ttf(root)
    preview_files = sorted(prev_dir.glob("*.png")) if prev_dir.is_dir() else []
    preview_by_name = {p.name: p for p in preview_files}

    lines: list[str] = [
        "# ebook-fonts 字体预览",
        "",
        f"- **字体数量：** {len(fonts)}",
        f"- **预览图：** {len(preview_files)} 个（`{prev_dir.relative_to(repo_root)}`）",
        "",
    ]

    by_pub: dict[str, list[Path]] = defaultdict(list)
    for fp in fonts:
        by_pub[publisher_heading_key(fp, root)].append(fp)

    pub_order = sorted(by_pub.keys(), key=lambda s: (s == "根目录", s.lower()))

    lines.append("**出版方目录**（点击跳转到下方对应小节）")
    lines.append("")
    for pub in pub_order:
        aid = publisher_anchor_id(pub)
        lines.append(f"- [{pub}](#{aid})")
    lines.append("")
    lines.append("---")
    lines.append("")

    missing_preview: list[Path] = []
    matched_png_names: set[str] = set()

    for pub in pub_order:
        group = by_pub[pub]
        aid = publisher_anchor_id(pub)
        lines.append(f'<a id="{aid}"></a>')
        lines.append(f"## {pub}")
        lines.append("")
        emit_font_table(
            lines,
            group,
            readme_out=out_path,
            root=root,
            raw_base=raw_base,
            preview_by_name=preview_by_name,
            missing_preview=missing_preview,
        )

    for fp in fonts:
        p = find_preview_png_path(fp, preview_by_name)
        if p is not None:
            matched_png_names.add(p.name)

    used_names = matched_png_names
    orphans = [p for p in preview_files if p.name not in used_names]
    if orphans:
        lines.append("---")
        lines.append("")
        lines.append("## 未匹配到字体的预览文件")
        lines.append("")
        link_cells_o = [md_table_cell(p.name) for p in sorted(orphans, key=lambda x: x.name.lower())]
        img_cells_o = []
        for p in sorted(orphans, key=lambda x: x.name.lower()):
            img_u = markdown_asset_url(out_path, p)
            alt = p.stem.replace('"', "'")
            img_cells_o.append(f"![{md_table_cell(alt)}]({img_u})")
        n = len(link_cells_o)
        lines.append("| " + " | ".join(link_cells_o) + " |")
        lines.append("| " + " | ".join(["---"] * n) + " |")
        lines.append("| " + " | ".join(img_cells_o) + " |")
        lines.append("")
        lines.append("*（无对应 TTF 匹配，故不提供字体下载链）*")
        lines.append("")

    if missing_preview:
        lines.append("---")
        lines.append("")
        lines.append(f"## 尚无预览的字体（共 {len(missing_preview)} 个）")
        lines.append("")
        lines.append(
            "*可运行 `python scripts/render_font_previews_html.py` 生成预览。*"
        )
        lines.append("")
        for fp in sorted(missing_preview, key=lambda p: str(p).lower()):
            rel = fp.resolve().relative_to(root)
            lines.append(f"- `{rel.as_posix()}`")
            if raw_base:
                raw_ttf = f"{raw_base}/{encode_repo_rel_path(rel)}"
                lines.append(f"  - [点击下载]({raw_ttf})")
            else:
                lines.append(
                    f"  - [{md_table_cell(fp.stem)}]({markdown_asset_url(out_path, fp)})"
                )
        lines.append("")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
