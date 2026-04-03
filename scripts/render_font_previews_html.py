#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用 test.html 的版式与正文，为每个 .ttf 生成一张纵向拼接的多页渲染预览图（PNG）。

默认做「接近 E-Ink」的效果：暖灰纸色、略偏冷的墨迹、灰阶量化后再映射到纸/墨双色；
可用 --no-eink 恢复纯白底直出。

通过 PyMuPDF 的 Story（MuPDF HTML/CSS 子集）排版并写入 PDF，再按页光栅化后垂直拼接；
不依赖 Playwright 或其它浏览器自动化。每次运行会先清空 `--out` 目录内已有文件与子目录。

依赖：
  pip install pymupdf Pillow

用法:
  python scripts/render_font_previews_html.py
  python scripts/render_font_previews_html.py --no-eink
  python scripts/render_font_previews_html.py --eink-levels 12
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    print("请先安装: pip install pymupdf", file=sys.stderr)
    sys.exit(1)

try:
    from PIL import Image, ImageOps
except ImportError:
    print("请先安装: pip install Pillow", file=sys.stderr)
    sys.exit(1)

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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


def build_preview_html(
    template: str,
    font_family: str,
    font_relpath_posix: str,
    *,
    eink: bool,
) -> str:
    # font_relpath_posix: 相对 --root 的路径，正斜杠，供 Archive + url() 解析
    esc = font_relpath_posix.replace("\\", "/").replace("'", "%27")
    if eink:
        # 偏暖灰底 + 非纯黑字，接近常见阅读器/墨水屏纸感（MuPDF 仍会抗锯齿，后期再压灰阶）
        page_bg = "#ebe9e4"
        ink = "#1a1a1a"
    else:
        page_bg = "#ffffff"
        ink = "#000000"
    injected = f"""
<style id="font-preview-injected">
@font-face {{
  font-family: "{font_family}";
  src: url('{esc}') format('truetype');
  font-display: block;
}}
html {{
  background: {page_bg};
  margin: 0;
}}
body {{
  font-family: "{font_family}", serif !important;
  margin: 0;
  padding: 24px 32px 40px;
  max-width: 720px;
  box-sizing: border-box;
  background: {page_bg};
  color: {ink};
}}
.firstTitle {{
  font-size: 2.1rem;
  font-weight: 700;
  margin: 0 0 0.75em;
  color: {ink};
  text-align: center;
}}
.secondTitle {{
  font-size: 1.65rem;
  font-weight: 600;
  margin: 0 0 0.6em;
  color: {ink};
}}
.content {{
  font-size: 1.2rem;
  line-height: 1.75;
  margin: 0 0 1em;
  text-align: justify;
  text-indent: 2em;
  color: {ink};
  letter-spacing: 0.01em;
}}
</style>
"""
    lower = template.lower()
    head_close = lower.find("</head>")
    if head_close != -1:
        return template[:head_close] + injected + template[head_close:]
    return injected + template


def story_to_pdf_bytes(html: str, archive: fitz.Archive, page_size_pt: fitz.Rect, inset_pt: float) -> bytes:
    story = fitz.Story(html, archive=archive)
    inner = page_size_pt + (inset_pt, inset_pt, -inset_pt, -inset_pt)

    def rectfn(_rect_num: int, _filled: object) -> tuple[fitz.Rect, fitz.Rect, None]:
        return page_size_pt, inner, None

    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    story.write(writer, rectfn)
    writer.close()
    return buf.getvalue()


def _trim_page_bottom_blank(im: Image.Image, tolerance: int = 14) -> Image.Image:
    """仅去掉单页光栅图底部大块匀色空白，不动上/左/右（保留版式边距与页眉）。
    自最底行向上：仅当整行像素彼此接近（匀色）且与最底行整体色相近时才删行。
    """
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    if h < 2:
        return im
    px = im.load()
    tol = tolerance

    def row_mean(y: int) -> tuple[int, int, int]:
        return tuple(sum(px[x, y][i] for x in range(w)) // w for i in range(3))

    def pixel_near(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
        return sum(abs(a[i] - b[i]) for i in range(3)) <= tol * 3

    def row_uniform_and_near_ref(y: int, ref: tuple[int, int, int]) -> bool:
        ra = row_mean(y)
        if not pixel_near(ra, ref):
            return False
        return all(pixel_near(px[x, y], ra) for x in range(w))

    base_ref = row_mean(h - 1)
    y = h - 1
    while y >= 0 and row_uniform_and_near_ref(y, base_ref):
        y -= 1
    new_h = y + 1
    if new_h < 1 or new_h >= h:
        return im
    return im.crop((0, 0, w, new_h))


def _apply_eink_postprocess(
    im: Image.Image,
    *,
    gray_levels: int,
    ink: tuple[int, int, int],
    paper: tuple[int, int, int],
) -> Image.Image:
    """灰度量化 + 双色映射，模拟墨水屏有限灰阶与漫反射纸质。"""
    g = im.convert("L")
    if gray_levels >= 2:
        n = gray_levels - 1

        def quantize(p: int) -> int:
            return int(round(p / 255.0 * n) / n * 255)

        g = g.point(quantize)
    ink_hex = "#%02x%02x%02x" % ink
    paper_hex = "#%02x%02x%02x" % paper
    return ImageOps.colorize(g, black=ink_hex, white=paper_hex)


def pdf_bytes_to_stitched_png(
    pdf_bytes: bytes,
    out_path: Path,
    dpi: int,
    *,
    eink: bool,
    eink_gray_levels: int,
    eink_ink_rgb: tuple[int, int, int],
    eink_paper_rgb: tuple[int, int, int],
) -> None:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count < 1:
            raise RuntimeError("Story 未产生任何 PDF 页面")
        rows: list[Image.Image] = []
        max_w = 0
        for i in range(doc.page_count):
            pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
            im = pix.pil_image()
            # 只有 e-ink 时纸底与 PDF 默认白底可区分，才安全裁页底；纯白模式裁匀色会吃掉 body 的 padding
            if eink:
                im = _trim_page_bottom_blank(im)
            rows.append(im)
            max_w = max(max_w, im.width)
        total_h = sum(im.height for im in rows)
        bg = eink_paper_rgb if eink else (255, 255, 255)
        canvas = Image.new("RGB", (max_w, total_h), bg)
        y = 0
        for im in rows:
            canvas.paste(im, (0, y))
            y += im.height
        if eink:
            canvas = _apply_eink_postprocess(
                canvas,
                gray_levels=eink_gray_levels,
                ink=eink_ink_rgb,
                paper=eink_paper_rgb,
            )
        canvas.save(out_path, "PNG")
    finally:
        doc.close()


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="用 PyMuPDF Story 为每个 TTF 生成 test.html 风格预览图")
    parser.add_argument(
        "--html",
        type=Path,
        default=repo_root / "test.html",
        help="HTML 模板路径（默认：仓库根目录下的 test.html）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root,
        help="字体扫描根目录与 Archive 根目录（默认：仓库根目录）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo_root / "font_previews",
        help="PNG 输出目录（默认：仓库根目录下的 font_previews）",
    )
    parser.add_argument(
        "--page-width-pt",
        type=float,
        default=540.0,
        help="单页版心宽度（磅），约等于 720px@96dpi（默认：540）",
    )
    parser.add_argument(
        "--page-height-pt",
        type=float,
        default=792.0,
        help="单页高度（磅），美国 Letter 约 792（默认：792）",
    )
    parser.add_argument(
        "--inset-pt",
        type=float,
        default=0.0,
        help="正文相对页边的内边距（磅）；版式里已有 padding，默认 0",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=144,
        help="光栅化分辨率（默认：144，约等于 96×1.5）",
    )
    parser.add_argument(
        "--no-eink",
        action="store_true",
        help="关闭墨水屏风格（纯白底、纯黑字，不做灰阶量化）",
    )
    parser.add_argument(
        "--eink-levels",
        type=int,
        default=16,
        metavar="N",
        help="墨水屏灰阶层数，2–64（默认：16，越小越像早期阅读器）",
    )
    args = parser.parse_args()
    eink = not args.no_eink
    levels = max(2, min(64, args.eink_levels))
    # 略冷一点的「墨」+ 暖一点的「纸」，光栅后再映射到该色域
    eink_ink = (38, 38, 42)
    eink_paper = (232, 230, 224)

    html_path = args.html.resolve()
    root = args.root.resolve()
    out_dir = args.out.resolve()

    if not html_path.is_file():
        print(f"找不到 HTML 模板: {html_path}", file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        print(f"不是有效目录: {root}", file=sys.stderr)
        sys.exit(1)

    template = html_path.read_text(encoding="utf-8")
    fonts = sorted(root.rglob("*.ttf"))

    out_dir.mkdir(parents=True, exist_ok=True)
    for child in list(out_dir.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    if not fonts:
        print(f"在 {root} 下未找到 .ttf 文件")
        return

    archive = fitz.Archive(str(root))
    page_rect = fitz.Rect(0, 0, args.page_width_pt, args.page_height_pt)

    for i, font_path in enumerate(fonts):
        rel = font_path.resolve().relative_to(root)
        family = f"__PreviewFont_{i}"
        doc_html = build_preview_html(template, family, rel.as_posix(), eink=eink)
        try:
            pdf_bytes = story_to_pdf_bytes(doc_html, archive, page_rect, args.inset_pt)
        except Exception as e:
            print(f"跳过（Story 失败）{rel}: {e}", file=sys.stderr)
            continue
        rel_stem = "__".join(rel.parts)
        out_name = sanitize_filename(f"{Path(rel_stem).stem}.png")
        out_file = out_dir / out_name
        try:
            pdf_bytes_to_stitched_png(
                pdf_bytes,
                out_file,
                args.dpi,
                eink=eink,
                eink_gray_levels=levels,
                eink_ink_rgb=eink_ink,
                eink_paper_rgb=eink_paper,
            )
        except Exception as e:
            print(f"跳过（导出 PNG 失败）{rel}: {e}", file=sys.stderr)
            continue
        print(out_file)


if __name__ == "__main__":
    main()
