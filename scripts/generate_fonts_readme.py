#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
遍历仓库内所有 .ttf 与 font_previews/*.png，生成 Markdown。

预览图使用相对 README 的路径嵌入（`![]()`）；「点击下载」指向对应 **.ttf** 的 raw 直链（非预览 PNG）。

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
from pathlib import Path

# 与 scripts/render_font_previews_html.py 中逻辑保持一致（避免依赖 pymupdf）
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


def preview_png_basename(font_path: Path, root: Path) -> str:
    rel = font_path.resolve().relative_to(root)
    rel_stem = "__".join(rel.parts)
    return sanitize_filename(f"{Path(rel_stem).stem}.png")


def rglob_ttf(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.ttf"):
        if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in p.parts):
            continue
        out.append(p)
    return sorted(out, key=lambda x: str(x).lower())


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


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="生成 README：相对路径直接嵌入预览图，可选附 raw 直链")
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

    ]

    lines += [
        f"- **字体数量：** {len(fonts)}",
        f"- **预览图：** {len(preview_files)} 个（`{prev_dir.relative_to(repo_root)}`）",
        "",
        "---",
        "",
    ]

    missing_preview: list[Path] = []
    for fp in fonts:
        rel = fp.resolve().relative_to(root)
        png_name = preview_png_basename(fp, root)
        ppath = preview_by_name.get(png_name)
        disp = rel.as_posix()

        if ppath:
            alt = rel.stem.replace('"', "'").replace("]", "").replace("[", "")
            img_url = markdown_asset_url(out_path, ppath)
            lines.append(f"### `{disp}`")
            lines.append("")
            lines.append(f"![{alt}]({img_url})")
            lines.append("")
            if raw_base:
                rel_ttf = fp.resolve().relative_to(repo_root)
                raw_url = f"{raw_base}/{encode_repo_rel_path(rel_ttf)}"
                lines.append(f"Raw：[点击下载]({raw_url})")
                lines.append("")
                lines.append("---")
        else:
            missing_preview.append(fp)
            lines.append(f"### `{disp}`")
            lines.append("")
            lines.append("*（尚无预览图，可运行 `python scripts/render_font_previews_html.py`）*")
            lines.append("")
        lines.append("")

    used_names = {preview_png_basename(f, root) for f in fonts}
    orphans = [p for p in preview_files if p.name not in used_names]
    if orphans:
        lines.append("---")
        lines.append("")
        lines.append("## 未匹配到字体的预览文件")
        lines.append("")
        for p in sorted(orphans, key=lambda x: x.name.lower()):
            img_u = markdown_asset_url(out_path, p)
            alt = p.stem.replace('"', "'")
            lines.append(f"### `{p.name}`")
            lines.append("")
            lines.append(f"![{alt}]({img_u})")
            lines.append("")
            lines.append("*（无对应 TTF 匹配，故不提供字体下载链）*")
            lines.append("")

    if missing_preview:
        lines.append("---")
        lines.append("")
        lines.append(f"## 尚无预览的字体（共 {len(missing_preview)} 个）")
        lines.append("")
        for fp in missing_preview:
            rel = fp.resolve().relative_to(root)
            lines.append(f"- `{rel.as_posix()}`")
            if raw_base:
                raw_ttf = f"{raw_base}/{encode_repo_rel_path(rel)}"
                lines.append(f"  - [点击下载]({raw_ttf})")
        lines.append("")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
