from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
from PIL import Image


A4_HEIGHT_PT = 841.89
DEFAULT_INPUT = "不定名-李天命.pdf"
DEFAULT_OUTPUT = "不定名-李天命_内容修边.pdf"
DEFAULT_WORK_DIR = "_fix_edges_work"
DEFAULT_REPORT = "_fix_edges_content_report.csv"
DEFAULT_INSPECT_DIR = "_inspect_pages"
CACHE_METADATA = "_fix_edges_source.json"
PAGE_IMAGE_RE = re.compile(r"^page-(\d+)\.jpg$")
# Every file this program writes into a cache folder. Clearing a cache deletes
# only these, so a cache folder pointed at a real folder can never be wiped.
CACHE_FILE_RE = re.compile(
    r"^(?:page-\d+(?:_rot|_rgb|_ocr_preview)*\.(?:jpe?g|png|ppm|pbm|pgm|tiff?)"
    r"|_fix_edges_source\.json|_ocr_user_words\.txt)$"
)
CACHE_VERSION = 2
DEFAULT_RENDER_DPI = 600
DEFAULT_JOBS = 14
APP_NAME = "PDF裁边"
EDITS_VERSION = 1
JPEG_MAGIC = b"\xff\xd8"
_UNSET = object()

try:
    import fitz  # type: ignore

    HAS_FITZ = True
except ImportError:
    fitz = None  # type: ignore
    HAS_FITZ = False


class CancelledError(RuntimeError):
    """Raised when a long-running crop job is cancelled."""


class CancelFlag(Protocol):
    def is_set(self) -> bool: ...


ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class CropResult:
    left: int
    top: int
    right: int
    bottom: int
    safe_left: int
    safe_top: int
    safe_right: int
    safe_bottom: int
    note: str


@dataclass
class PagePlan:
    page: int
    path: Path
    source_path: Path
    width: int
    height: int
    mode: str
    crop: tuple[int, int, int, int]
    safe: tuple[int, int, int, int]
    note: str
    rotate_deg: float = 0.0


@dataclass
class CropJob:
    src: Path
    output: Path
    work: Path
    report: Path | None = None
    reuse: bool = True
    min_width_ratio: float = 0.86
    min_height_ratio: float = 0.86
    min_full_width_ratio: float = 0.90
    min_full_height_ratio: float = 0.90
    manual_crops: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)
    page_rotates: dict[int, float] = field(default_factory=dict)
    pages: list[int] | None = None
    inspect_pages: list[int] = field(default_factory=list)
    inspect_dir: Path | None = None
    inspect_dpi: int = 120
    jobs: int = 0
    render_dpi: int = DEFAULT_RENDER_DPI
    write_pdf: bool = True
    backend: str = "auto"
    crop_mode: str = "edges"
    auto_rotate: bool = True
    ocr: bool = False
    ocr_lang: str = "chi_tra+chi_sim+eng+jpn"
    ocr_txt: bool = False
    ocr_ref: Path | None = None


def check_cancelled(cancel: CancelFlag | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("cancelled")


def emit(progress: ProgressCallback | None, message: str, current: int = 0, total: int = 0) -> None:
    if progress is not None:
        progress(message, current, total)


def default_jobs() -> int:
    """Analysis is mostly GIL-bound numpy work: 14 workers beat 30 on a 14-thread CPU."""
    return max(1, min(DEFAULT_JOBS, os.cpu_count() or DEFAULT_JOBS))


def safe_stem(stem: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", stem).strip(" .")
    return cleaned or "pdf"


def default_output_for(src: Path) -> Path:
    return src.with_name(f"{src.stem}_裁边.pdf")


def default_report_for(output: Path) -> Path:
    return output.with_name(f"{output.stem}_报告.csv")


def app_data_dir(*parts: str) -> Path:
    """Per-user folder for page caches and saved edits, kept out of the PDF's own folder."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base, APP_NAME, *parts)


def source_key(src: Path) -> str:
    """Stem plus a fingerprint of size and head/tail bytes, so a moved book keeps its cache and edits."""
    digest = hashlib.sha1()
    try:
        size = src.stat().st_size
        digest.update(str(size).encode("ascii"))
        with src.open("rb") as handle:
            digest.update(handle.read(1 << 20))
            if size > 2 << 20:
                handle.seek(-(1 << 20), os.SEEK_END)
                digest.update(handle.read(1 << 20))
    except OSError:
        digest.update(str(src.resolve()).lower().encode("utf-8"))
    return f"{safe_stem(src.stem)[:60]}-{digest.hexdigest()[:10]}"


def default_work_dir_for(src: Path) -> Path:
    return app_data_dir("cache", source_key(src))


def clear_work_dir(work: Path) -> None:
    """Empty a page-image cache, deleting only files this program writes there.

    Unknown files and sub-folders are left alone, so a --work-dir / 缓存目录 that
    points at a real folder can never be wiped.
    """
    if not work.exists():
        work.mkdir(parents=True)
        return
    for entry in work.iterdir():
        if entry.is_file() and CACHE_FILE_RE.match(entry.name):
            entry.unlink()


def _cache_files(root: Path):
    if not root.is_dir():
        return
    for book in root.iterdir():
        if book.is_dir():
            for entry in book.iterdir():
                if entry.is_file() and CACHE_FILE_RE.match(entry.name):
                    yield entry


def cache_usage(root: Path | None = None) -> tuple[int, int]:
    """(file count, bytes) of cached page images under the per-user cache folder."""
    files = list(_cache_files(root or app_data_dir("cache")))
    return len(files), sum(entry.stat().st_size for entry in files)


def clear_cache_root(root: Path | None = None) -> tuple[int, int]:
    """Delete every cached page image under the per-user cache folder; returns (files, bytes)."""
    root = root or app_data_dir("cache")
    files = total = 0
    for entry in list(_cache_files(root)):
        total += entry.stat().st_size
        entry.unlink()
        files += 1
    if root.is_dir():
        for book in root.iterdir():
            if book.is_dir():
                try:
                    book.rmdir()
                except OSError:
                    pass
    return files, total


def edits_path_for(src: Path) -> Path:
    return app_data_dir("edits", source_key(src) + ".json")


def load_manual_edits(src: Path) -> tuple[dict[int, tuple[int, int, int, int]], dict[int, float]]:
    """Manual crop boxes and rotations the desktop app saved for this PDF."""
    try:
        data = json.loads(edits_path_for(src).read_text(encoding="utf-8"))
        crops = {int(page): tuple(int(v) for v in box) for page, box in data.get("crops", {}).items()}
        rotates = {int(page): float(angle) for page, angle in data.get("rotates", {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, {}
    return {page: box for page, box in crops.items() if len(box) == 4}, rotates


def save_manual_edits(
    src: Path,
    crops: dict[int, tuple[int, int, int, int]],
    rotates: dict[int, float],
) -> None:
    path = edits_path_for(src)
    if not crops and not rotates:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": EDITS_VERSION,
        "source": str(src.resolve()),
        "crops": {str(page): list(box) for page, box in sorted(crops.items())},
        "rotates": {str(page): angle for page, angle in sorted(rotates.items())},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def run(cmd: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required command not found: {cmd[0]}. Install Poppler and add its bin directory to PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        cmd_text = " ".join(str(part) for part in cmd)
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            raise RuntimeError(f"Command failed: {cmd_text}\n{detail}") from exc
        raise RuntimeError(f"Command failed: {cmd_text}") from exc
    return proc.stdout


def page_count_fitz(pdf: Path) -> int:
    if not HAS_FITZ:
        raise RuntimeError("PyMuPDF is not installed")
    with fitz.open(pdf) as doc:
        return int(doc.page_count)


def page_count_poppler(pdf: Path, cwd: Path) -> int:
    out = run(["pdfinfo", str(pdf)], cwd)
    match = re.search(r"^Pages:\s+(\d+)", out, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Could not read page count from pdfinfo output")
    return int(match.group(1))


def page_count(pdf: Path, cwd: Path, backend: str = "auto") -> int:
    want = backend.lower()
    errors: list[str] = []
    if want in {"auto", "pymupdf", "fitz"}:
        try:
            return page_count_fitz(pdf)
        except Exception as exc:
            errors.append(f"PyMuPDF: {exc}")
            if want != "auto":
                raise
    if want in {"auto", "poppler"}:
        try:
            return page_count_poppler(pdf, cwd)
        except Exception as exc:
            errors.append(f"Poppler: {exc}")
            if want != "auto":
                raise
    detail = " | ".join(errors) if errors else "no backend available"
    raise RuntimeError(f"Could not read page count from {pdf.name}: {detail}")


def page_image_index(path: Path) -> int:
    match = PAGE_IMAGE_RE.match(path.name)
    if not match:
        raise ValueError(f"Not an original page image name: {path.name}")
    return int(match.group(1))


def page_number_from_image(path: Path) -> int:
    return page_image_index(path) + 1


def page_image_path(work: Path, page_num: int) -> Path:
    return work / f"page-{page_num - 1:03d}.jpg"


def page_images(work: Path) -> list[Path]:
    if not work.exists():
        return []
    return sorted(
        (p for p in work.iterdir() if p.is_file() and PAGE_IMAGE_RE.match(p.name)),
        key=page_image_index,
    )


def source_metadata(src: Path, page_total: int, render_dpi: int = DEFAULT_RENDER_DPI) -> dict[str, object]:
    stat = src.stat()
    return {
        "version": CACHE_VERSION,
        "source": str(src.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "pages": page_total,
        "render_dpi": render_dpi,
    }


def read_cache_metadata(work: Path) -> dict[str, object] | None:
    try:
        data = json.loads((work / CACHE_METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_cache_metadata(work: Path, src: Path, page_total: int, render_dpi: int = DEFAULT_RENDER_DPI) -> None:
    metadata = source_metadata(src, page_total, render_dpi)
    (work / CACHE_METADATA).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def cache_matches_source(
    work: Path,
    src: Path,
    page_total: int,
    render_dpi: int | None = None,
) -> bool:
    data = read_cache_metadata(work)
    if not data:
        return False
    expected = source_metadata(src, page_total, render_dpi or DEFAULT_RENDER_DPI)
    for key in ("source", "size", "mtime_ns", "pages"):
        if data.get(key) != expected.get(key):
            return False
    if int(data.get("version", 1)) >= 2 and render_dpi is not None:
        cached_dpi = data.get("render_dpi")
        if cached_dpi is not None and int(cached_dpi) != int(render_dpi):
            return False
    return True


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def validate_work_dir(work: Path, cwd: Path) -> None:
    resolved_work = work.resolve()
    resolved_cwd = cwd.resolve()
    if resolved_work.exists() and not resolved_work.is_dir():
        raise RuntimeError(f"--work-dir exists but is not a directory: {work}")
    if resolved_work == Path(resolved_work.anchor):
        raise RuntimeError("--work-dir must be a dedicated cache directory, not a drive root")
    if is_relative_to(resolved_cwd, resolved_work):
        raise RuntimeError("--work-dir must not be the current PDF directory or one of its parents")


def _image_cover_ratio(page: object, xref: int) -> float:
    area = float(page.rect.width * page.rect.height)
    if area <= 0:
        return 0.0
    try:
        rects = page.get_image_rects(xref)
    except Exception:
        rects = []
    if not rects:
        return 0.0
    return max((float(r.width * r.height) / area) for r in rects)


def _pixmap_to_jpeg(pix: object, dest: Path) -> None:
    current = pix
    try:
        if getattr(current, "alpha", False):
            current = fitz.Pixmap(fitz.csRGB, current)
        elif getattr(current, "n", 3) != 3:
            current = fitz.Pixmap(fitz.csRGB, current)
        dest.parent.mkdir(parents=True, exist_ok=True)
        current.save(dest.as_posix(), jpg_quality=95)
    finally:
        if current is not pix:
            current = None


def _export_embedded_image(doc: object, page: object, dest: Path) -> bool:
    images = page.get_images(full=True)
    if not images:
        return False
    best = None
    best_cover = -1.0
    for img in images:
        xref = int(img[0])
        cover = _image_cover_ratio(page, xref)
        if cover > best_cover:
            best = img
            best_cover = cover
    if best is None or best_cover < 0.75:
        return False
    xref = int(best[0])
    info = doc.extract_image(xref)
    if info and info.get("image") and str(info.get("ext", "")).lower() in {"jpg", "jpeg"}:
        dest.write_bytes(info["image"])
        return True
    try:
        pix = fitz.Pixmap(doc, xref)
        try:
            _pixmap_to_jpeg(pix, dest)
        finally:
            pix = None
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _render_page_jpeg(page: object, dest: Path, render_dpi: int) -> None:
    matrix = fitz.Matrix(render_dpi / 72.0, render_dpi / 72.0)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    try:
        _pixmap_to_jpeg(pix, dest)
    finally:
        pix = None


def extract_images_fitz(
    src: Path,
    work: Path,
    pages: list[int],
    render_dpi: int,
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
) -> None:
    if not HAS_FITZ:
        raise RuntimeError("PyMuPDF is not installed")
    work.mkdir(parents=True, exist_ok=True)
    total = len(pages)
    with fitz.open(src) as doc:
        for i, page_num in enumerate(pages, start=1):
            check_cancelled(cancel)
            if page_num < 1 or page_num > doc.page_count:
                raise RuntimeError(f"Page {page_num} is outside the {doc.page_count}-page PDF")
            dest = page_image_path(work, page_num)
            page = doc[page_num - 1]
            if not _export_embedded_image(doc, page, dest):
                _render_page_jpeg(page, dest, render_dpi)
            emit(progress, f"抽出第 {page_num} 页", i, total)


def extract_images_poppler(
    src: Path,
    work: Path,
    cwd: Path,
    expected_pages: int | None,
) -> list[Path]:
    clear_work_dir(work)
    run(["pdfimages", "-j", str(src), str(work / "page")], cwd)
    images = page_images(work)
    if not images:
        raise RuntimeError("pdfimages did not extract any JPEG images")
    if expected_pages is not None and len(images) != expected_pages:
        raise RuntimeError(
            f"Page/image count mismatch: {expected_pages} pages, {len(images)} extracted images"
        )
    return images


def extract_images(
    src: Path,
    work: Path,
    cwd: Path,
    reuse: bool = False,
    expected_pages: int | None = None,
    pages: list[int] | None = None,
    render_dpi: int = DEFAULT_RENDER_DPI,
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
    backend: str = "auto",
) -> list[Path]:
    validate_work_dir(work, cwd)
    needed = list(pages) if pages else (list(range(1, expected_pages + 1)) if expected_pages else None)
    cache_ok = bool(
        reuse
        and work.exists()
        and expected_pages is not None
        and cache_matches_source(work, src, expected_pages, render_dpi)
    )
    if cache_ok and needed is not None:
        have = {page_number_from_image(p): p for p in page_images(work)}
        missing = [page for page in needed if page not in have or not have[page].is_file()]
        if not missing:
            return [have[page] for page in needed]
    elif reuse and work.exists() and needed is not None:
        have = {page_number_from_image(p): p for p in page_images(work)}
        if (
            expected_pages is not None
            and len(have) == expected_pages
            and cache_matches_source(work, src, expected_pages, render_dpi)
        ):
            return [have[page] for page in needed]
        if not cache_matches_source(work, src, expected_pages or 0, render_dpi):
            print(
                "cached page images are missing source metadata or do not match this PDF; extracting again",
                file=sys.stderr,
            )
            emit(progress, "缓存与当前 PDF 不匹配，重新抽图", 0, 0)

    want = backend.lower()
    use_fitz = want in {"auto", "pymupdf", "fitz"} and HAS_FITZ
    if use_fitz:
        if not cache_ok:
            # A cache that does not match this PDF is stale for every page, so a
            # page subset must not pick up another book's page-NNN.jpg either.
            clear_work_dir(work)
        if needed is None:
            if expected_pages is None:
                expected_pages = page_count(src, cwd, backend="pymupdf")
            needed = list(range(1, expected_pages + 1))
        have = {page_number_from_image(p): p for p in page_images(work)} if work.exists() else {}
        missing = [page for page in needed if page not in have or not have[page].is_file()]
        if missing:
            extract_images_fitz(src, work, missing, render_dpi, progress, cancel)
        if expected_pages is not None:
            write_cache_metadata(work, src, expected_pages, render_dpi)
        have = {page_number_from_image(p): p for p in page_images(work)}
        missing = [page for page in needed if page not in have]
        if missing:
            raise RuntimeError(f"Failed to extract page image(s): {missing[:12]}")
        return [have[page] for page in needed]

    if want not in {"auto", "poppler"}:
        raise RuntimeError("PyMuPDF is required for the selected backend")
    if pages:
        raise RuntimeError("Poppler extraction cannot pull selected pages only; install PyMuPDF or process the whole PDF")
    emit(progress, "使用 Poppler 抽出页面图", 0, 0)
    images = extract_images_poppler(src, work, cwd, expected_pages)
    if expected_pages is not None:
        write_cache_metadata(work, src, expected_pages, render_dpi)
    return images


def load_rgb_array(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        if im.mode == "RGB":
            return np.asarray(im, dtype=np.uint8)
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def luminance(arr: np.ndarray) -> np.ndarray:
    return (
        0.2126 * arr[:, :, 0].astype(np.float32)
        + 0.7152 * arr[:, :, 1].astype(np.float32)
        + 0.0722 * arr[:, :, 2].astype(np.float32)
    )


def saturation(arr: np.ndarray) -> np.ndarray:
    return arr.max(axis=2).astype(np.int16) - arr.min(axis=2).astype(np.int16)


def _band_is_full_page_text(lum: np.ndarray, start: int, end: int) -> bool:
    """True only for a real captured facing page, not 水墨 or a 页内标识."""
    if end - start < 8:
        return False
    height = lum.shape[0]
    ink = lum[:, start:end] < 160
    top = float(ink[: height // 2].mean())
    bottom = float(ink[height // 2 :].mean())
    if top < 0.012 or bottom < 0.012:
        return False
    if max(top, bottom) > 0 and min(top, bottom) / max(top, bottom) < 0.30:
        return False
    row_hit = float((ink.mean(axis=1) > 0.008).mean())
    if row_hit < 0.45:
        return False
    col = ink.mean(axis=0)
    if float((col > 0.02).mean()) < 0.80:
        return False
    total = float(col.sum()) + 1e-6
    order = np.sort(col)[::-1]
    cum = np.cumsum(order)
    narrow = int(np.searchsorted(cum, 0.80 * total)) + 1
    if narrow / max(col.size, 1) < 0.40:
        return False
    return True


def facing_page_cut(lum: np.ndarray) -> tuple[int, int]:
    """Drop a captured facing page on the left or right of a two-page scan."""
    height, width = lum.shape
    if width < 220 or height < 220:
        return 0, width
    ink = (lum < 160).mean(axis=0)
    win = max(7, width // 90)
    smooth = np.convolve(ink, np.ones(win, dtype=np.float64) / win, mode="same")
    low = smooth < 0.022
    min_run = max(12, int(round(width * 0.02)))
    min_face = max(24, int(round(width * 0.14)))

    def gutter_run(start: int, end: int) -> tuple[int, int] | None:
        run = _longest_true_run(low[start:end])
        if run is None:
            return None
        a, b = start + run[0], start + run[1]
        if b - a < min_run:
            return None
        paper = float((lum[:, a:b] > 210).mean())
        if paper < 0.90:
            return None
        left_ink = float(smooth[max(0, a - max(20, width // 20)) : a].max()) if a > 4 else 0.0
        right_ink = float(smooth[b : min(width, b + max(20, width // 20))].max()) if b < width - 4 else 0.0
        if left_ink < 0.045 or right_ink < 0.045:
            return None
        return a, b

    left, right = 0, width
    right_gutter = gutter_run(int(width * 0.52), int(width * 0.92))
    if right_gutter is not None:
        g0, g1 = right_gutter
        if (
            g1 >= int(width * 0.58)
            and width - g1 >= min_face
            and _band_is_full_page_text(lum, g1, width)
            and _band_is_full_page_text(lum, 0, g0)
        ):
            right = (g0 + g1) // 2
    left_gutter = gutter_run(int(width * 0.08), int(width * 0.48))
    if left_gutter is not None:
        g0, g1 = left_gutter
        if (
            g0 <= int(width * 0.42)
            and g0 >= min_face
            and _band_is_full_page_text(lum, 0, g0)
            and _band_is_full_page_text(lum, g1, width)
        ):
            left = (g0 + g1) // 2
    if right - left < int(width * 0.50):
        return 0, width
    return left, right


def page_sliver_cut(lum: np.ndarray) -> tuple[int, int]:
    """Trim a thin previous/next-page remnant sitting outside a paper gutter."""
    height, width = lum.shape
    if width < 160:
        return 0, width
    paper = (lum > 210).mean(axis=0)
    ink = (lum < 170).mean(axis=0)
    max_sliver = max(18, int(width * 0.12))
    min_gutter = max(8, int(width * 0.018))

    def first_gutter(paper_line: np.ndarray, ink_line: np.ndarray) -> tuple[int, int] | None:
        start = None
        limit = min(len(paper_line), max_sliver + min_gutter + 24)
        for i in range(limit):
            if paper_line[i] >= 0.96 and ink_line[i] < 0.012:
                if start is None:
                    start = i
            elif start is not None:
                if i - start >= min_gutter and start <= max_sliver:
                    return start, i
                start = None
        if start is not None and limit - start >= min_gutter and start <= max_sliver:
            return start, limit
        return None

    def sliver_is_tall(band: np.ndarray) -> bool:
        """Facing-page leftovers run down the page; a 课次标签 is a short printed tab."""
        if band.size == 0:
            return False
        if band.ndim == 2 and band.shape[1] > 12:
            left_dark = float((band[:, :6] < 170).mean())
            right_dark = float((band[:, -6:] < 170).mean())
            band = band[:, 6:] if left_dark >= right_dark else band[:, :-6]
        if band.size == 0:
            return False
        rows = (band < 170).mean(axis=1) > 0.015
        return float(rows.mean()) >= 0.16

    def is_edge_sliver(
        outer_ink: np.ndarray,
        sliver_ink: np.ndarray,
        after_ink: np.ndarray,
        sliver_lum: float,
        sliver_band: np.ndarray,
    ) -> bool:
        if sliver_ink.size < 6:
            return False
        if float(outer_ink.max()) < 0.004:
            return False
        if sliver_lum <= 188:
            return False
        if float(sliver_ink.max()) >= 0.12:
            return False
        if after_ink.size == 0 or float(after_ink.max()) < 0.04:
            return False
        if not sliver_is_tall(sliver_band):
            return False
        return True

    left, right = 0, width
    gutter = first_gutter(paper, ink)
    if gutter is not None:
        g0, g1 = gutter
        if is_edge_sliver(
            ink[:6],
            ink[:g0],
            ink[g1 : min(width, g1 + max(40, width // 8))],
            float(lum[:, : max(g0, 1)].mean()),
            lum[:, : max(g0, 1)],
        ):
            left = min(g0 + 3, (g0 + g1) // 2)
    gutter = first_gutter(paper[::-1], ink[::-1])
    if gutter is not None:
        g0, g1 = gutter
        start = width - g1
        end = width - g0
        if is_edge_sliver(
            ink[-6:],
            ink[end:],
            ink[max(0, start - max(40, width // 8)) : start],
            float(lum[:, min(end, width - 1) :].mean()),
            lum[:, min(end, width - 1) :],
        ):
            right = max(end - 3, (start + end) // 2)

    def sliced_remnant(values_ink: np.ndarray, values_paper: np.ndarray, values_lum: np.ndarray) -> int | None:
        """Cut a razor-thin leftover column from the facing page, not a 页内标识."""
        search = max(18, int(width * 0.045))
        max_strip = max(14, int(width * 0.016))
        i = 0
        while i < search and values_ink[i] < 0.10:
            i += 1
        if i >= search or i > max(12, int(width * 0.018)):
            return None
        j = i
        while j < search and values_ink[j] >= 0.10:
            j += 1
        if not (4 <= j - i <= max_strip):
            return None
        if float(values_lum[:, i:j].mean()) <= 185:
            return None
        if not sliver_is_tall(values_lum[:, i:j]):
            return None
        g0 = j
        while g0 < min(len(values_paper), j + 8) and not (
            values_paper[g0] >= 0.96 and values_ink[g0] < 0.012
        ):
            g0 += 1
        g1 = g0
        while (
            g1 < min(len(values_paper), g0 + max(30, int(width * 0.08)))
            and values_paper[g1] >= 0.96
            and values_ink[g1] < 0.012
        ):
            g1 += 1
        if g1 - g0 < max(8, int(width * 0.018)):
            return None
        after = values_ink[g1 : min(len(values_ink), g1 + max(40, width // 8))]
        if after.size == 0 or float(after.max()) < 0.04:
            return None
        return (g0 + g1) // 2

    if left == 0:
        hit = sliced_remnant(ink, paper, lum)
        if hit is not None:
            left = hit
    if right == width:
        hit = sliced_remnant(ink[::-1], paper[::-1], lum[:, ::-1])
        if hit is not None:
            right = width - hit
    if right - left < int(width * 0.55):
        if left > 0 and (width - right) >= left:
            right = width
        elif right < width:
            left = 0
        else:
            return 0, width
    return left, right


def dirty_border_bounds(
    arr: np.ndarray,
    lum: np.ndarray | None = None,
    sat: np.ndarray | None = None,
) -> tuple[int, int, int, int]:
    """Trim outer scan dirt and binding strips. Stop at real paper; keep content."""
    h, w = arr.shape[:2]
    if lum is None:
        lum = luminance(arr)
    if sat is None:
        sat = saturation(arr)
    inner = lum[h // 5 : max(h // 5 + 1, 4 * h // 5), w // 5 : max(w // 5 + 1, 4 * w // 5)]
    paper = float(np.median(inner)) if inner.size else float(np.percentile(lum, 80))
    if paper < 140:
        return 0, 0, w, h

    paper_px = lum >= (paper - 15.0)
    col_mean = lum.mean(axis=0)
    row_mean = lum.mean(axis=1)
    col_paper = paper_px.mean(axis=0)
    row_paper = paper_px.mean(axis=1)
    col_sat = sat.mean(axis=0).astype(np.float32)
    row_sat = sat.mean(axis=1).astype(np.float32)
    shade_mean = paper - 18.0

    def is_dirt(mean_v: float, paper_frac: float, sat_v: float) -> bool:
        shaded = mean_v < shade_mean and paper_frac < 0.70
        inset_band = paper_frac < 0.58 and mean_v < paper - 10.0
        tinted = sat_v > 18.0 and paper_frac < 0.72 and mean_v < paper - 8.0
        return shaded or inset_band or tinted

    def dirt_flags(mean_line: np.ndarray, paper_line: np.ndarray, sat_line: np.ndarray, limit: int) -> list[bool]:
        n = min(limit, int(mean_line.size))
        return [
            is_dirt(float(mean_line[i]), float(paper_line[i]), float(sat_line[i]))
            for i in range(n)
        ]

    def trim_start(mean_line: np.ndarray, paper_line: np.ndarray, sat_line: np.ndarray, limit: int) -> int:
        flags = dirt_flags(mean_line, paper_line, sat_line, limit)
        if not flags:
            return 0
        first = next((i for i, flag in enumerate(flags) if flag), None)
        if first is None or first > 40:
            return 0
        last = first
        for i in range(first, len(flags)):
            if flags[i]:
                last = i + 1
            else:
                break
        if last - first <= 2 and first >= 20:
            return 0
        if last - first > 50:
            return 0
        if first >= 6 and float(paper_line[:first].mean()) < 0.82:
            return 0
        return min(limit, last + 2)

    def trim_end(mean_line: np.ndarray, paper_line: np.ndarray, sat_line: np.ndarray, limit: int) -> int:
        n = int(mean_line.size)
        flags = dirt_flags(mean_line[::-1], paper_line[::-1], sat_line[::-1], limit)
        if not flags:
            return n
        first = next((i for i, flag in enumerate(flags) if flag), None)
        if first is None or first > 40:
            return n
        last = first
        for i in range(first, len(flags)):
            if flags[i]:
                last = i + 1
            else:
                break
        if last - first <= 2 and first >= 20:
            return n
        if last - first > 50:
            return n
        if first >= 6 and float(paper_line[::-1][:first].mean()) < 0.82:
            return n
        return max(0, n - min(limit, last + 2))

    def keep_side_is_artwork(cut: int, side: str) -> bool:
        """True if the kept page next to `cut` is 水墨/brush/课标题, not text or gutter.

        Stops dirt/sliver/facing trims from shaving an illustration or a printed
        chapter banner that reaches the paper edge.
        """
        vertical = side in {"top", "bottom"}
        limit = h if vertical else w
        if cut <= 0 or cut >= limit:
            return False
        pad = max(32, int(round((h if vertical else w) * 0.038)))
        if side == "left":
            start, end = cut, min(w, cut + pad)
            dark = lum[:, start:end] < (paper - 45.0)
        elif side == "right":
            start, end = max(0, cut - pad), cut
            dark = lum[:, start:end] < (paper - 45.0)
        elif side == "top":
            start, end = cut, min(h, cut + pad)
            dark = lum[start:end, :] < (paper - 45.0)
        else:
            start, end = max(0, cut - pad), cut
            dark = lum[start:end, :] < (paper - 45.0)
        if end - start < 12:
            return False
        wide8 = 0
        wide20 = 0
        for row in dark:
            best = cur = 0
            for pix in row:
                if pix:
                    cur += 1
                    if cur > best:
                        best = cur
                else:
                    cur = 0
            if best >= 8:
                wide8 += 1
            if best >= 20:
                wide20 += 1
        if wide20 < 40:
            return False
        if wide20 >= 70:
            return True
        return wide8 > 0 and wide20 / wide8 >= 0.50

    max_side = min(110, max(12, int(round(w * 0.08))))
    max_vert = min(80, max(10, int(round(h * 0.045))))
    left = trim_start(col_mean, col_paper, col_sat, max_side)
    right = trim_end(col_mean, col_paper, col_sat, max_side)
    top = trim_start(row_mean, row_paper, row_sat, max_vert)
    bottom = trim_end(row_mean, row_paper, row_sat, max_vert)

    def illustration_in_band(a: int, b: int, axis: str) -> bool:
        if b - a < 6:
            return False
        if axis == "y" and float((sat[a:b, :] > 28).mean()) >= 0.18:
            return True
        band = lum[:, a:b] if axis == "x" else lum[a:b, :]
        dark = band < paper - 45.0
        if axis == "x":
            cover = float((dark.mean(axis=1) > 0.12).mean())
            lines = dark
        else:
            cover = float((dark.mean(axis=0) > 0.12).mean())
            lines = dark.T
        wide = 0
        for line in lines:
            best = cur = 0
            for pix in line:
                if pix:
                    cur += 1
                    if cur > best:
                        best = cur
                else:
                    cur = 0
            if best >= 8:
                wide += 1
        return cover < 0.65 and wide >= 40

    if left and illustration_in_band(0, left, "x"):
        left = 0
    if right < w and illustration_in_band(right, w, "x"):
        right = w
    if top and illustration_in_band(0, top, "y"):
        top = 0
    if bottom < h and illustration_in_band(bottom, h, "y"):
        bottom = h
    if right - left < w * 0.86 or bottom - top < h * 0.88:
        left, top, right, bottom = 0, 0, w, h
    face_left, face_right = facing_page_cut(lum)
    face_owns_left = face_left > left
    face_owns_right = face_right < right
    if face_owns_left:
        left = face_left
    if face_owns_right:
        right = face_right
    sliver_left, sliver_right = page_sliver_cut(lum)
    if sliver_left > left:
        left = sliver_left
    if sliver_right < right:
        right = sliver_right
    if left and keep_side_is_artwork(left, "left"):
        left = 0
    if right < w and keep_side_is_artwork(right, "right"):
        right = w
    if top and keep_side_is_artwork(top, "top"):
        top = 0
    if bottom < h and keep_side_is_artwork(bottom, "bottom"):
        bottom = h
    # A facing-page cut owns its side. A dirt or sliver trim that runs well past
    # the binding line or shadow is pulled back (the sliver test often mistakes
    # the line for a page remnant); covers and colour-cast pages are left alone.
    if float((sat > 40).mean()) < 0.15:
        if left > 0 and not face_owns_left:
            left = reduce_edge_overtrim(lum, paper, left)
        if right < w and not face_owns_right:
            right = w - reduce_edge_overtrim(lum[:, ::-1], paper, w - right)
    if right - left < w * 0.45:
        return 0, 0, w, h
    return left, top, right, bottom


def reduce_edge_overtrim(lum: np.ndarray, paper: float, cur: int) -> int:
    """Pull a side trim back to just past the edge line or shadow it was meant to remove.

    `lum` is oriented so the edge under test is column 0 (pass lum[:, ::-1] for
    the right edge) and `cur` is the trim chosen by the dirt detectors. The
    shaded-column test often runs 4-8% of the width into clean paper; readers
    who corrected such pages by hand cut about 2% past the end of the line or
    shadow instead. The result is never larger than `cur`, so this step can
    only keep more of the page.
    """
    h, w = lum.shape
    if w < 200 or h < 200 or paper < 200 or cur <= 0 or cur > w * 0.10:
        return cur
    reach = min(w // 3, int(round(w * 0.12)))
    body = lum[int(h * 0.04) : int(h * 0.96), :reach]
    dark = body < paper - 60.0
    col = dark.mean(axis=0)
    half = dark.shape[0] // 2
    col_top = dark[:half].mean(axis=0)
    col_bottom = dark[half:].mean(axis=0)
    grey = (body < paper - 25.0).mean(axis=0)

    # Full-height dark line starting near the edge.
    line_end = 0
    zone = int(round(w * 0.04))
    start = next(
        (x for x in range(min(zone, reach)) if col[x] >= 0.15 and min(col_top[x], col_bottom[x]) >= 0.06),
        None,
    )
    if start is not None:
        gap = max(4, int(round(w * 0.0045)))
        line_end = start + 1
        for x in range(start, reach):
            if col[x] >= 0.15:
                line_end = x + 1
            elif x - line_end >= gap:
                break
        if line_end - start > w * 0.015:
            # A wide shadow band counts only its first stretch.
            line_end = min(line_end, start + int(round(w * 0.02)))

    # Faint grey structure (shadow, broken line) touching the edge.
    grey_end = 0
    miss = 0
    gap = max(3, int(round(w * 0.003)))
    for x in range(min(reach, int(w * 0.10))):
        if grey[x] >= 0.04:
            grey_end = x + 1
            miss = 0
        else:
            miss += 1
            if miss > gap and (grey_end > 0 or x > w * 0.02):
                break

    if line_end:
        edge_end = max(line_end, min(grey_end, line_end + int(round(w * 0.01))))
    else:
        edge_end = grey_end
    if edge_end <= 0 or cur < edge_end:
        return cur
    target = int(round(edge_end + w * 0.02))
    return target if cur > target else cur


def middle_slice(length: int, trim_ratio: float) -> slice:
    trim = min(length // 3, max(0, int(round(length * trim_ratio))))
    return slice(trim, max(trim + 1, length - trim))


def edge_artifact_bounds(
    arr: np.ndarray,
    lum: np.ndarray | None = None,
    sat: np.ndarray | None = None,
) -> tuple[int, int, int, int]:
    """Return a conservative rectangle that drops scan-edge artifacts.

    The test is intentionally stricter at the outer edge and ignores the
    opposite page's top/bottom bands while judging left/right edges. This keeps
    sparse title pages broad, but removes binding shadows, black top bands, and
    facing-page text strips before content bounds are measured.
    """
    h, w = arr.shape[:2]
    if lum is None:
        lum = luminance(arr)
    if sat is None:
        sat = saturation(arr)
    bg = float(np.percentile(lum, 90))
    shade = lum < min(244.0, max(190.0, bg - 14.0))
    paper = ((lum > 188) & (sat < 90)) | (lum > 224)
    mark = (lum < 150) | ((sat > 45) & (lum < 230))

    y_mid = middle_slice(h, 0.05)
    x_mid = middle_slice(w, 0.05)
    col_paper = paper[y_mid, :].mean(axis=0)
    col_mark = mark[y_mid, :].sum(axis=0)
    col_shade = shade[y_mid, :].sum(axis=0)
    row_paper = paper[:, x_mid].mean(axis=1)
    row_mark = mark[:, x_mid].sum(axis=1)

    col_mark_thr = max(10, int(round((y_mid.stop - y_mid.start) * 0.020)))
    row_mark_thr = max(12, int(round((x_mid.stop - x_mid.start) * 0.035)))
    col_win = max(10, min(28, w // 55))
    row_win = max(10, min(28, h // 70))
    x_pad = max(3, min(8, w // 180))
    y_pad = max(3, min(8, h // 220))
    side_zone = max(col_win * 2, min(int(round(w * 0.075)), max(col_win, w // 8)))

    side_bad = (
        (col_paper < 0.82)
        | (col_mark > col_mark_thr * 2.0)
        | (col_shade > col_mark_thr * 2.0)
    )

    def clean_col(start: int, end: int) -> bool:
        return (
            float(np.percentile(col_paper[start:end], 20)) >= 0.965
            and float(np.percentile(col_mark[start:end], 80)) <= col_mark_thr
        )

    def dirty_col(start: int, end: int) -> bool:
        return (
            float(np.percentile(col_paper[start:end], 20)) < 0.94
            or float(np.percentile(col_mark[start:end], 80)) > col_mark_thr
            or float(np.max(col_mark[start:end])) > col_mark_thr * 2.0
            or float(np.max(col_shade[start:end])) > col_mark_thr * 2.0
        )

    def clean_row(start: int, end: int) -> bool:
        return (
            float(np.percentile(row_paper[start:end], 20)) >= 0.93
            and float(np.percentile(row_mark[start:end], 80)) <= row_mark_thr
        )

    def dirty_row(start: int, end: int) -> bool:
        return (
            float(np.percentile(row_paper[start:end], 20)) < 0.90
            or float(np.percentile(row_mark[start:end], 80)) > row_mark_thr
            or float(np.max(row_mark[start:end])) > row_mark_thr * 2.0
        )

    def content_like_columns(start: int, end: int) -> bool:
        """Detect real text/table marks in a side band before discarding it."""
        if end - start < col_win * 2:
            return False
        zone = mark[y_mid, start:end]
        zone_h, zone_w = zone.shape
        if zone_h == 0 or zone_w == 0:
            return False
        col_thr = max(4, int(round(zone_h * 0.006)))
        row_thr = max(3, int(round(zone_w * 0.018)))
        col_counts = zone.sum(axis=0)
        row_counts = zone.sum(axis=1)
        col_hits = col_counts >= col_thr
        row_hits = row_counts >= row_thr
        distributed_text = (
            int(col_hits.sum()) >= max(12, int(round(zone.shape[1] * 0.10)))
            and int(row_hits.sum()) >= max(8, int(round(zone.shape[0] * 0.04)))
        )
        vertical_rule = int((col_counts >= max(col_thr * 3, int(round(zone_h * 0.12)))).sum()) >= max(
            1, int(round(zone_w * 0.015))
        )
        row_spread = int((row_counts >= max(2, int(round(zone_w * 0.012)))).sum()) >= max(
            10, int(round(zone_h * 0.06))
        )
        marked_density = float(zone.mean())
        return distributed_text or (vertical_rule and row_spread) or (
            marked_density >= 0.010
            and row_spread
            and int(col_hits.sum()) >= max(4, int(round(zone_w * 0.035)))
        )

    full_col_dark = (lum < 105).mean(axis=0)

    def soft_outer_content(start: int, end: int) -> bool:
        """Keep low-density text/table marks that reach the scan edge.

        Narrow artifact trimming is meant for dense binding shadows or black
        scan borders. Rotated tables can put real text and rules inside the
        outer 50-70 px, and those bands are broad but not solid-black.
        """
        if end - start < col_win:
            return False
        if not content_like_columns(start, end):
            return False
        dark_band = full_col_dark[start:end]
        if dark_band.size == 0:
            return False
        dark_hits = int((dark_band > 0.012).sum())
        if dark_hits < max(8, int(round((end - start) * 0.18))):
            return False
        return (
            float(np.percentile(dark_band, 95)) < 0.14
            and float(np.max(dark_band)) < 0.22
        )

    def narrow_left_artifact() -> int:
        max_trim = max(col_win, min(70, int(round(w * 0.045))))
        if soft_outer_content(0, max_trim):
            return 0
        dirty = full_col_dark[:max_trim] > 0.012
        last_dirty = -1
        clean_run = 0
        for x, is_dirty in enumerate(dirty):
            if bool(is_dirty):
                last_dirty = x
                clean_run = 0
            elif last_dirty >= 0:
                clean_run += 1
                if clean_run >= max(4, col_win // 2):
                    break
        return min(max_trim, last_dirty + x_pad + 1) if last_dirty >= 0 else 0

    def narrow_right_artifact() -> int:
        max_trim = max(col_win, min(70, int(round(w * 0.045))))
        if soft_outer_content(w - max_trim, w):
            return w
        dirty = full_col_dark[w - max_trim :] > 0.012
        last_dirty = -1
        clean_run = 0
        for off, is_dirty in enumerate(dirty[::-1]):
            if bool(is_dirty):
                last_dirty = off
                clean_run = 0
            elif last_dirty >= 0:
                clean_run += 1
                if clean_run >= max(4, col_win // 2):
                    break
        return max(0, w - (last_dirty + x_pad + 1)) if last_dirty >= 0 else w

    left = 0
    left_bad = np.flatnonzero(side_bad[:side_zone])
    if dirty_col(0, col_win) or left_bad.size:
        for x in range(0, max(1, min(int(w * 0.24), w - col_win))):
            if clean_col(x, x + col_win) and (not left_bad.size or x > int(left_bad.max())):
                left = min(w, x + x_pad)
                break

    right = w
    right_bad = np.flatnonzero(side_bad[w - side_zone :]) + (w - side_zone)
    if dirty_col(w - col_win, w) or right_bad.size:
        for off in range(0, max(1, min(int(w * 0.27), w - col_win))):
            r = w - off
            if clean_col(r - col_win, r) and (not right_bad.size or r <= int(right_bad.min())):
                right = max(0, r - x_pad)
                break

    top = 0
    if dirty_row(0, row_win):
        for y in range(0, max(1, min(int(h * 0.13), h - row_win))):
            if clean_row(y, y + row_win):
                top = min(h, y + y_pad)
                break

    bottom = h
    if dirty_row(h - row_win, h):
        for off in range(0, max(1, min(int(h * 0.13), h - row_win))):
            b = h - off
            if clean_row(b - row_win, b):
                bottom = max(0, b - y_pad)
                break

    left_narrow = narrow_left_artifact()
    right_narrow = narrow_right_artifact()
    side_guard = max(col_win, min(70, int(round(w * 0.035))))
    protected_side_guard = max(16, min(side_guard, int(round(w * 0.022))))
    large_side_trim = max(int(round(side_guard * 1.5)), int(round(w * 0.07)))
    if left > large_side_trim and content_like_columns(side_guard, left):
        left_protect = 0 if soft_outer_content(0, side_guard) else protected_side_guard
        left = max(left_narrow, left_protect)
    if w - right >= large_side_trim and content_like_columns(right, w - side_guard):
        right_protect = w if soft_outer_content(w - side_guard, w) else w - protected_side_guard
        right = min(right_narrow, right_protect)

    left = max(left, left_narrow)
    right = min(right, right_narrow)

    if right - left < w * 0.65 or bottom - top < h * 0.65:
        return 0, 0, w, h
    return left, top, right, bottom


def expand_interval(start: int, end: int, limit_start: int, limit_end: int, target: int) -> tuple[int, int]:
    target = min(target, limit_end - limit_start)
    if end - start >= target:
        return start, end
    center = (start + end) / 2.0
    new_start = int(round(center - target / 2))
    new_end = new_start + target
    if new_start < limit_start:
        new_start = limit_start
        new_end = new_start + target
    if new_end > limit_end:
        new_end = limit_end
        new_start = new_end - target
    return max(limit_start, new_start), min(limit_end, new_end)


def content_crop(
    path: Path,
    page_num: int,
    min_width_ratio: float = 0.86,
    min_height_ratio: float = 0.86,
    min_full_width_ratio: float = 0.90,
    min_full_height_ratio: float = 0.90,
    crop_mode: str = "edges",
    *,
    arr: np.ndarray | None = None,
    lum: np.ndarray | None = None,
    panel: object = _UNSET,
) -> CropResult:
    """Crop box for one page image; pass arr/lum/panel when the caller already computed them."""
    if arr is None:
        arr = load_rgb_array(path)
    h, w = arr.shape[:2]
    if lum is None:
        lum = luminance(arr)

    object_box = isolated_object_bounds(lum, arr, panel=panel)
    if object_box is not None:
        left, top, right, bottom, kind = object_box
        return CropResult(left, top, right, bottom, left, top, right, bottom, kind)

    dark_frac = float(np.mean(lum < 120))
    if float(np.median(lum)) < 170 or dark_frac > 0.28:
        return CropResult(0, 0, w, h, 0, 0, w, h, "dark-page-kept")

    sat = saturation(arr)
    if crop_mode == "content":
        safe_left, safe_top, safe_right, safe_bottom = edge_artifact_bounds(arr, lum, sat)
    else:
        safe_left, safe_top, safe_right, safe_bottom = dirty_border_bounds(arr, lum, sat)
    if crop_mode != "content":
        note = (
            "dirty-edges-trimmed"
            if (safe_left, safe_top, safe_right, safe_bottom) != (0, 0, w, h)
            else "clean-page-kept"
        )
        return CropResult(
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            note,
        )
    safe_lum = lum[safe_top:safe_bottom, safe_left:safe_right]
    bg = float(np.percentile(safe_lum, 88)) if safe_lum.size else float(np.percentile(lum, 88))
    dark_thr = min(182.0, max(118.0, bg - 42.0))
    mask = (lum < dark_thr) | ((sat > 55) & (lum < 210))

    ignore_x = max(18, min(48, int(round(w * 0.028))))
    ignore_y = max(8, min(36, int(round(h * 0.012))))
    inner_x = max(2, min(ignore_x, max(2, (safe_right - safe_left) // 30)))
    inner_y = max(2, min(ignore_y, max(2, (safe_bottom - safe_top) // 50)))
    detect_left = min(safe_right, safe_left + inner_x)
    detect_right = max(safe_left, safe_right - inner_x)
    detect_top = min(safe_bottom, safe_top + inner_y)
    detect_bottom = max(safe_top, safe_bottom - inner_y)
    if detect_right <= detect_left or detect_bottom <= detect_top:
        return CropResult(
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            "edge-only-cropped",
        )

    usable = np.zeros_like(mask, dtype=bool)
    usable[detect_top:detect_bottom, detect_left:detect_right] = mask[
        detect_top:detect_bottom, detect_left:detect_right
    ]
    row_count = usable.sum(axis=1)
    col_count = usable.sum(axis=0)
    row_thr = max(5, int(round((detect_right - detect_left) * 0.0030)))
    col_thr = max(5, int(round((detect_bottom - detect_top) * 0.0028)))
    rows = np.flatnonzero(row_count >= row_thr)
    cols = np.flatnonzero(col_count >= col_thr)
    if rows.size == 0 or cols.size == 0:
        safe_w = safe_right - safe_left
        safe_h = safe_bottom - safe_top
        blank_w = max(
            int(round(safe_w * min_width_ratio)),
            min(int(round(w * min_full_width_ratio)), safe_w),
        )
        blank_h = max(
            int(round(safe_h * min_height_ratio)),
            min(int(round(h * min_full_height_ratio)), safe_h),
        )
        center_x = (safe_left + safe_right) // 2
        center_y = (safe_top + safe_bottom) // 2
        left, right = expand_interval(center_x, center_x, safe_left, safe_right, blank_w)
        top, bottom = expand_interval(center_y, center_y, safe_top, safe_bottom, blank_h)
        note = "blank-expanded"
        if (safe_left, safe_top, safe_right, safe_bottom) != (0, 0, w, h):
            note += "+edge-trimmed"
        return CropResult(
            left,
            top,
            right,
            bottom,
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            note,
        )

    left = int(cols[0])
    right = int(cols[-1]) + 1
    top = int(rows[0])
    bottom = int(rows[-1]) + 1

    pad_probe_x = max(18, int(round(w * 0.025)))
    pad_probe_y = max(18, int(round(h * 0.018)))
    x0 = max(detect_left, left - pad_probe_x)
    x1 = min(detect_right, right + pad_probe_x)
    y0 = max(detect_top, top - pad_probe_y)
    y1 = min(detect_bottom, bottom + pad_probe_y)
    ys, xs = np.nonzero(mask[y0:y1, x0:x1])
    if xs.size and ys.size:
        left = min(left, x0 + int(xs.min()))
        right = max(right, x0 + int(xs.max()) + 1)
        top = min(top, y0 + int(ys.min()))
        bottom = max(bottom, y0 + int(ys.max()) + 1)

    pad_x = max(26, int(round(w * 0.035)))
    pad_y = max(30, int(round(h * 0.028)))
    left = max(safe_left, left - pad_x)
    right = min(safe_right, right + pad_x)
    top = max(safe_top, top - pad_y)
    bottom = min(safe_bottom, bottom + pad_y)

    if right - left < w * 0.08 or bottom - top < h * 0.05:
        return CropResult(
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
            "edge-only-cropped",
        )

    note = "content-cropped"
    safe_w = safe_right - safe_left
    safe_h = safe_bottom - safe_top
    min_w = int(round(safe_w * min_width_ratio))
    min_h = int(round(safe_h * min_height_ratio))
    if right - left < min_w or bottom - top < min_h:
        left, right = expand_interval(left, right, safe_left, safe_right, min_w)
        top, bottom = expand_interval(top, bottom, safe_top, safe_bottom, min_h)
        note = "content-expanded"
    full_guarded = False
    guarded_w = min(int(round(w * min_full_width_ratio)), safe_w)
    guarded_h = min(int(round(h * min_full_height_ratio)), safe_h)
    if right - left < guarded_w:
        old_left, old_right = left, right
        left, right = expand_interval(left, right, safe_left, safe_right, guarded_w)
        full_guarded = full_guarded or (left, right) != (old_left, old_right)
    if bottom - top < guarded_h:
        old_top, old_bottom = top, bottom
        top, bottom = expand_interval(top, bottom, safe_top, safe_bottom, guarded_h)
        full_guarded = full_guarded or (top, bottom) != (old_top, old_bottom)
    if full_guarded:
        note += "+full-guard"
    if (safe_left, safe_top, safe_right, safe_bottom) != (0, 0, w, h):
        note += "+edge-trimmed"

    return CropResult(
        left,
        top,
        right,
        bottom,
        safe_left,
        safe_top,
        safe_right,
        safe_bottom,
        note,
    )


def _fit_edge_angle(ys: np.ndarray, xs: np.ndarray) -> tuple[float, float]:
    if ys.size < 20:
        return 0.0, 1e9
    matrix = np.vstack([ys, np.ones_like(ys)]).T
    slope, intercept = np.linalg.lstsq(matrix, xs, rcond=None)[0]
    pred = slope * ys + intercept
    resid = float(np.mean((xs - pred) ** 2))
    return float(np.degrees(np.arctan(slope))), resid


def _score_column_profile(image_u8: np.ndarray, angle: float, y0: int, y1: int) -> float:
    rot = np.array(
        Image.fromarray(image_u8).rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0),
        dtype=np.float32,
    )
    return float(np.var(rot[y0:y1].mean(axis=0)))


def _score_spine_edges(gx_u8: np.ndarray, angle: float, y0: int, y1: int) -> float:
    rot = np.array(
        Image.fromarray(gx_u8).rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0),
        dtype=np.float32,
    )
    profile = rot[y0:y1].mean(axis=0)
    return float(np.var(profile) + 0.15 * (float(profile.max()) ** 2) / 100.0)


def _score_row_profile(image_u8: np.ndarray, angle: float) -> float:
    rot = np.array(
        Image.fromarray(image_u8).rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0),
        dtype=np.float32,
    )
    return float(np.var(rot.mean(axis=1)))


def _search_deskew_angle(
    score_at: Callable[[float], float],
    *,
    bound: float = 3.0,
    coarse: float = 0.2,
    need_ratio: float = 1.10,
) -> float:
    zero = score_at(0.0)
    best_ang = 0.0
    best = zero
    step = max(1, int(round(coarse * 10.0)))
    limit = int(round(bound * 10.0))
    for tenth in range(-limit, limit + 1, step):
        ang = tenth / 10.0
        score = score_at(ang)
        if score > best:
            best = score
            best_ang = ang
    refine_from = int(round(best_ang * 10.0)) - 3
    refine_to = int(round(best_ang * 10.0)) + 3
    for tenth in range(refine_from, refine_to + 1):
        ang = tenth / 10.0
        if abs(ang) > bound + 1e-6:
            continue
        score = score_at(ang)
        if score > best:
            best = score
            best_ang = ang
    if abs(best_ang) < 0.25 or best < zero * need_ratio:
        return 0.0
    if abs(abs(best_ang) - bound) < 1e-6:
        return 0.0
    return float(round(best_ang, 1))


def _longest_true_run(flags: np.ndarray) -> tuple[int, int] | None:
    if flags.size == 0 or not bool(flags.any()):
        return None
    padded = np.concatenate([[False], np.asarray(flags, dtype=bool), [False]])
    diff = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    if starts.size == 0:
        return None
    index = int(np.argmax(ends - starts))
    return int(starts[index]), int(ends[index])


def detect_isolated_dark_panel(lum: np.ndarray, max_width_ratio: float = 0.70) -> tuple[int, int] | None:
    """Return (x0, x1) of a lone tall dark panel on a white scanned page."""
    height, width = lum.shape
    paper = float(np.percentile(lum, 88))
    if paper < 200:
        return None
    threshold = min(175.0, paper - 40.0)
    col_dark = (lum < threshold).mean(axis=0)
    if float(col_dark.max()) < 0.55:
        return None
    run = _longest_true_run(col_dark > 0.18)
    if run is None:
        return None
    x0, x1 = run
    band_w = x1 - x0
    if band_w < 20 or band_w > int(width * max_width_ratio):
        return None
    if x0 < int(width * 0.08) or x1 > int(width * 0.92):
        return None
    band = lum[:, x0:x1]
    if float((band < 180).mean()) < 0.70:
        return None
    outside = np.ones(width, dtype=bool)
    outside[x0:x1] = False
    if bool(outside.any()) and float((lum[:, outside] < 180).mean()) > 0.04:
        return None
    if float(((band < 180).mean(axis=1) > 0.25).mean()) < 0.70:
        return None
    if float(col_dark[x0:x1].mean()) < 0.55:
        return None
    return x0, x1


def detect_isolated_spine(lum: np.ndarray) -> tuple[int, int] | None:
    """Return (x0, x1) of a lone tall spine on a white scanned page."""
    band = detect_isolated_dark_panel(lum, max_width_ratio=0.38)
    if band is None:
        return None
    height = lum.shape[0]
    x0, x1 = band
    if height < (x1 - x0) * 3:
        return None
    return x0, x1


def _resize_gray(lum: np.ndarray, width: int, height: int) -> np.ndarray:
    return np.array(
        Image.fromarray(lum.astype(np.uint8)).resize((width, height), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )


def spine_outer_x_range(lum: np.ndarray, x0: int, x1: int, arr: np.ndarray | None = None) -> tuple[int, int]:
    """Keep the whole spine: left bevel, printed face, and right crease.

    Drop only the white field and the extra adjacent-cover strip past the
    thin highlight on the right edge.
    """
    del arr
    height, width = lum.shape
    band_w = x1 - x0
    if band_w <= 0:
        return x0, x1
    paper = float(np.percentile(lum, 88))
    threshold = min(175.0, paper - 40.0)
    col_dark = (lum < threshold).mean(axis=0)
    edge = max(8, height // 120)
    top_ok = (lum[:edge] < threshold).mean(axis=0) >= 0.75
    bot_ok = (lum[-edge:] < threshold).mean(axis=0) >= 0.75
    slack = max(12, band_w // 10)
    search_left = max(0, x0 - slack)
    search_right = min(width, x1 + slack)

    def full_height(x: int) -> bool:
        return bool(col_dark[x] >= 0.70 and top_ok[x] and bot_ok[x])

    left = x0
    for x in range(search_left, search_right):
        if full_height(x):
            left = x
            break
    right = x1
    for x in range(search_right - 1, search_left - 1, -1):
        if full_height(x):
            right = x + 1
            break
    if right - left < max(20, int(band_w * 0.45)):
        left, right = x0, x1

    y0, y1 = int(height * 0.15), int(height * 0.85)
    profile = lum[y0:y1, left:right].mean(axis=0)
    zone = int(profile.size * 0.68)
    highlight = None
    best_score = 0.0
    for i in range(zone + 4, max(zone + 5, profile.size - 4)):
        value = float(profile[i])
        left_mean = float(profile[max(0, i - 14) : i].mean())
        score = value - left_mean
        if score > 10.0 and value >= float(profile[i - 2 : i + 3].max()) - 1e-3:
            if score > best_score:
                best_score = score
                highlight = i
    if highlight is not None:
        right = min(right, left + highlight + max(6, band_w // 80))

    pad = max(2, int(round(band_w * 0.006)))
    left = max(0, left - pad)
    right = min(width, right + pad)
    return left, right


def panel_outer_x_range(lum: np.ndarray, x0: int, x1: int) -> tuple[int, int]:
    height, width = lum.shape
    paper = float(np.percentile(lum, 88))
    threshold = min(175.0, paper - 40.0)
    col_dark = (lum < threshold).mean(axis=0)
    edge = max(8, height // 120)
    top_ok = (lum[:edge] < threshold).mean(axis=0) >= 0.70
    bot_ok = (lum[-edge:] < threshold).mean(axis=0) >= 0.70
    slack = max(12, (x1 - x0) // 12)
    search_left = max(0, x0 - slack)
    search_right = min(width, x1 + slack)
    left = x0
    for x in range(search_left, search_right):
        if col_dark[x] >= 0.65 and top_ok[x] and bot_ok[x]:
            left = x
            break
    right = x1
    for x in range(search_right - 1, search_left - 1, -1):
        if col_dark[x] >= 0.65 and top_ok[x] and bot_ok[x]:
            right = x + 1
            break
    pad = max(2, int(round((x1 - x0) * 0.006)))
    return max(0, left - pad), min(width, right + pad)


def isolated_object_bounds(
    lum: np.ndarray, arr: np.ndarray | None = None, panel: object = _UNSET
) -> tuple[int, int, int, int, str] | None:
    """Crop an isolated dark panel or spine, dropping the surrounding white field."""
    if panel is _UNSET:
        panel = detect_isolated_dark_panel(lum)
    if panel is None:
        return None
    height, width = lum.shape
    x0, x1 = panel
    spine_like = (x1 - x0) <= int(width * 0.38) and height >= (x1 - x0) * 3
    if spine_like:
        left, right = spine_outer_x_range(lum, x0, x1, arr)
        kind = "spine-cropped"
    else:
        left, right = panel_outer_x_range(lum, x0, x1)
        kind = "panel-cropped"
    paper = float(np.percentile(lum, 88))
    threshold = min(175.0, paper - 40.0)
    band_h = (lum[:, x0:x1] < threshold).mean(axis=1)
    row_run = _longest_true_run(band_h > 0.20)
    if row_run is None:
        top, bottom = 0, height
    else:
        top, bottom = row_run
    pad_y = max(2, int(round(height * 0.001)))
    top = max(0, top - pad_y)
    bottom = min(height, bottom + pad_y)
    if right - left < 20 or bottom - top < int(height * 0.55):
        return None
    return left, top, right, bottom, kind


def estimate_spine_angle(lum: np.ndarray, spine: object = _UNSET) -> float | None:
    """Deskew a scanned book-spine page.

    Work on a downscaled band so high-DPI scans do not let the already-straight
    outer crop box drown the tilted printed face.
    """
    xs = detect_isolated_spine(lum) if spine is _UNSET else spine
    if xs is None:
        return None
    height, _width = lum.shape
    x0, x1 = xs
    band = lum[:, x0:x1]
    target_h = 1800
    if height > target_h:
        small_w = max(40, int(round(band.shape[1] * target_h / height)))
        small = _resize_gray(band, small_w, target_h)
    else:
        small = band.astype(np.float32)
        target_h = small.shape[0]
    gx = np.abs(np.gradient(small, axis=1))
    margin = max(4, int(round(small.shape[1] * 0.03)))
    gx[:, :margin] = 0
    gx[:, -margin:] = 0
    if float(gx.max()) < 4.0:
        return None
    gx_u8 = np.clip(gx * 3.0, 0, 255).astype(np.uint8)
    y0, y1 = int(target_h * 0.08), int(target_h * 0.92)
    angle = _search_deskew_angle(
        lambda ang: _score_spine_edges(gx_u8, ang, y0, y1),
        bound=5.0,
        coarse=0.2,
        need_ratio=1.08,
    )
    if abs(angle) > 8.0:
        return None
    return float(np.clip(angle, -8.0, 8.0))


def estimate_text_skew(lum: np.ndarray) -> float:
    height, width = lum.shape
    if height < 80 or width < 80:
        return 0.0
    scale = min(1.0, 360.0 / max(height, width))
    small_h = max(40, int(round(height * scale)))
    small_w = max(40, int(round(width * scale)))
    small = np.array(
        Image.fromarray(lum.astype(np.uint8)).resize((small_w, small_h), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )
    ink = small < max(110.0, float(np.percentile(small, 30)))
    ink_frac = float(ink.mean())
    if ink_frac < 0.01 or ink_frac > 0.45:
        return 0.0
    ink_u8 = (ink.astype(np.uint8) * 255)
    row_ang = _search_deskew_angle(
        lambda ang: _score_row_profile(ink_u8, ang),
        bound=3.0,
        coarse=0.2,
        need_ratio=1.12,
    )
    col_ang = _search_deskew_angle(
        lambda ang: _score_column_profile(ink_u8, ang, 0, ink_u8.shape[0]),
        bound=3.0,
        coarse=0.2,
        need_ratio=1.12,
    )
    if abs(row_ang) < 1e-6:
        return col_ang
    if abs(col_ang) < 1e-6:
        return row_ang
    if row_ang * col_ang < 0:
        return 0.0
    return row_ang if abs(row_ang) >= abs(col_ang) else col_ang


def spine_from_panel(lum: np.ndarray, panel: tuple[int, int] | None) -> tuple[int, int] | None:
    """Same answer as detect_isolated_spine(), derived from an already detected dark panel."""
    if panel is None:
        return None
    height, width = lum.shape
    x0, x1 = panel
    if x1 - x0 > int(width * 0.38) or height < (x1 - x0) * 3:
        return None
    return panel


def estimate_deskew_from_lum(lum: np.ndarray, panel: object = _UNSET) -> float:
    spine = detect_isolated_spine(lum) if panel is _UNSET else spine_from_panel(lum, panel)
    angle = estimate_spine_angle(lum, spine=spine)
    if angle is not None:
        return angle
    return estimate_text_skew(lum)


def estimate_deskew_degrees(path: Path) -> float:
    return estimate_deskew_from_lum(luminance(load_rgb_array(path)))


def _inscribed_crop_after_rotate(
    src_w: int, src_h: int, dest_w: int, dest_h: int, angle_deg: float
) -> tuple[int, int, int, int]:
    theta = abs(math.radians(angle_deg))
    cosine = abs(math.cos(theta))
    if cosine < 0.2:
        return 0, 0, dest_w, dest_h
    inner_w = src_w * cosine
    inner_h = src_h * cosine
    left = max(0, int(math.floor((dest_w - inner_w) / 2.0)))
    top = max(0, int(math.floor((dest_h - inner_h) / 2.0)))
    right = min(dest_w, int(math.ceil(left + inner_w)))
    bottom = min(dest_h, int(math.ceil(top + inner_h)))
    if right - left < src_w * 0.7 or bottom - top < src_h * 0.7:
        return 0, 0, dest_w, dest_h
    return left, top, right, bottom


def rotate_image_file(src: Path, dest: Path, angle: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        rgb = im if im.mode == "RGB" else im.convert("RGB")
        src_w, src_h = rgb.size
        rotated = rgb.rotate(
            angle,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=(255, 255, 255),
        )
        box = _inscribed_crop_after_rotate(src_w, src_h, rotated.size[0], rotated.size[1], angle)
        if box != (0, 0, rotated.size[0], rotated.size[1]):
            rotated = rotated.crop(box)
        rotated.save(dest, format="JPEG", quality=92, subsampling=0)
    return dest


def ensure_jpeg_file(path: Path) -> Path:
    with path.open("rb") as handle:
        magic = handle.read(2)
    if magic == JPEG_MAGIC:
        return path
    dest = path.with_suffix(".jpg")
    if dest == path:
        dest = path.with_name(path.stem + "_rgb.jpg")
    with Image.open(path) as im:
        rgb = im if im.mode == "RGB" else im.convert("RGB")
        rgb.save(dest, format="JPEG", quality=95, subsampling=0, optimize=True)
    return dest


def analyze_one_page(
    image: Path,
    page_num: int,
    min_width_ratio: float,
    min_height_ratio: float,
    min_full_width_ratio: float,
    min_full_height_ratio: float,
    manual_crop: tuple[int, int, int, int] | None = None,
    rotate_deg: float = 0.0,
    crop_mode: str = "edges",
    auto_rotate: bool = False,
) -> PagePlan:
    work_image = image
    angle = float(rotate_deg or 0.0)
    auto_note = ""
    reuse: dict[str, object] = {}
    if abs(angle) < 1e-6 and auto_rotate:
        # Decode once: when the page stays unrotated, the crop pass reuses the
        # deskew pass's pixels, luminance and dark-panel detection.
        arr = load_rgb_array(image)
        lum = luminance(arr)
        panel = detect_isolated_dark_panel(lum)
        angle = float(estimate_deskew_from_lum(lum, panel) or 0.0)
        if abs(angle) >= 0.25:
            auto_note = f"+auto-rotate-{angle:.1f}"
        if abs(angle) <= 1e-6:
            reuse = {"arr": arr, "lum": lum, "panel": panel}
    if abs(angle) > 1e-6:
        work_image = rotate_image_file(image, image.with_name(f"{image.stem}_rot.jpg"), angle)
    with Image.open(work_image) as im:
        width, height = im.size
        mode = im.mode
    crop = content_crop(
        work_image,
        page_num,
        min_width_ratio,
        min_height_ratio,
        min_full_width_ratio,
        min_full_height_ratio,
        crop_mode,
        **reuse,
    )
    if manual_crop is not None:
        box = clamp_crop(manual_crop, width, height)
        note = f"manual-override+auto-{crop.note}"
    else:
        box = (crop.left, crop.top, crop.right, crop.bottom)
        note = crop.note
    if auto_note:
        note += auto_note
    return PagePlan(
        page=page_num,
        path=work_image,
        source_path=image,
        width=width,
        height=height,
        mode=mode,
        crop=box,
        safe=(crop.safe_left, crop.safe_top, crop.safe_right, crop.safe_bottom),
        note=note,
        rotate_deg=angle,
    )


def analyze_pages(
    images: list[Path],
    min_width_ratio: float,
    min_height_ratio: float,
    manual_crops: dict[int, tuple[int, int, int, int]] | None = None,
    min_full_width_ratio: float = 0.90,
    min_full_height_ratio: float = 0.90,
    page_rotates: dict[int, float] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
    jobs: int = 1,
    crop_mode: str = "edges",
    auto_rotate: bool = False,
) -> list[PagePlan]:
    manual_crops = manual_crops or {}
    page_rotates = page_rotates or {}
    items = [(page_number_from_image(image), image) for image in images]
    total = len(items)

    def run_one(page_num: int, image: Path) -> PagePlan:
        check_cancelled(cancel)
        return analyze_one_page(
            image,
            page_num,
            min_width_ratio,
            min_height_ratio,
            min_full_width_ratio,
            min_full_height_ratio,
            manual_crops.get(page_num),
            float(page_rotates.get(page_num, 0.0) or 0.0),
            crop_mode,
            auto_rotate and page_num not in page_rotates,
        )

    workers = jobs if jobs > 0 else default_jobs()
    if workers == 1 or total <= 2:
        plans: list[PagePlan] = []
        for index, (page_num, image) in enumerate(items, start=1):
            plans.append(run_one(page_num, image))
            emit(progress, f"分析第 {page_num} 页", index, total)
        return plans

    ordered: list[PagePlan | None] = [None] * total
    index_by_page = {page_num: i for i, (page_num, _) in enumerate(items)}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one, page_num, image): page_num for page_num, image in items}
        try:
            for future in as_completed(futures):
                check_cancelled(cancel)
                page_num = futures[future]
                ordered[index_by_page[page_num]] = future.result()
                done += 1
                emit(progress, f"分析第 {page_num} 页", done, total)
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return [plan for plan in ordered if plan is not None]


def pdf_num(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-5):
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def color_space(mode: str) -> tuple[str, str]:
    if mode == "L":
        return "/DeviceGray", ""
    if mode == "CMYK":
        return "/DeviceCMYK", " /Decode [1 0 1 0 1 0 1 0]"
    return "/DeviceRGB", ""


def write_report(pages: list[PagePlan], report: Path) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "page",
                "image",
                "width_px",
                "height_px",
                "left_px",
                "top_px",
                "right_px",
                "bottom_px",
                "crop_left_px",
                "crop_top_px",
                "crop_right_px",
                "crop_bottom_px",
                "safe_left_px",
                "safe_top_px",
                "safe_right_px",
                "safe_bottom_px",
                "note",
                "rotate_deg",
            ]
        )
        for page in pages:
            left, top, right, bottom = page.crop
            safe_left, safe_top, safe_right, safe_bottom = page.safe
            writer.writerow(
                [
                    page.page,
                    page.path.name,
                    page.width,
                    page.height,
                    left,
                    top,
                    right,
                    bottom,
                    left,
                    top,
                    page.width - right,
                    page.height - bottom,
                    safe_left,
                    safe_top,
                    safe_right,
                    safe_bottom,
                    page.note,
                    page.rotate_deg,
                ]
            )


def write_pdf_from_plans(
    pages: list[PagePlan],
    output: Path,
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
    source: Path | None = None,
) -> None:
    """Write the cropped PDF; with `source`, also carry over its bookmarks, page labels and title."""
    if not pages:
        raise RuntimeError("No pages to write")
    output.parent.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[PagePlan, Path, str]] = []
    for page in pages:
        check_cancelled(cancel)
        jpeg_path = ensure_jpeg_file(page.path)
        with Image.open(jpeg_path) as im:
            mode = im.mode
        prepared.append((page, jpeg_path, mode))
    tmp = output.with_suffix(".tmp.pdf")
    try:
        _write_image_pdf(tmp, prepared, progress, cancel)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(output)
    if source is not None:
        copy_document_outline(source, output, pages)


def copy_document_outline(src: Path, output: Path, pages: list[PagePlan]) -> None:
    """Carry bookmarks, page labels and title/author from the source PDF into the cropped PDF."""
    if not HAS_FITZ:
        return
    try:
        with fitz.open(src) as source:
            toc = source.get_toc(simple=True)
            labels = source.get_page_labels()
            meta = {
                key: value
                for key, value in (source.metadata or {}).items()
                if key in ("title", "author", "subject", "keywords") and value
            }
            source_pages = source.page_count
        index = {plan.page: i + 1 for i, plan in enumerate(pages)}
        outline: list[list] = []
        for level, title, page, *_rest in toc:
            if page in index:
                # Dropped pages can orphan a child entry; keep the hierarchy valid.
                level = max(1, min(int(level), outline[-1][0] + 1 if outline else 1))
                outline.append([level, title, index[page]])
        same_pages = [plan.page for plan in pages] == list(range(1, source_pages + 1))
        if not outline and not (labels and same_pages) and not meta:
            return
        rewrite: Path | None = None
        doc = fitz.open(output)
        try:
            if outline:
                doc.set_toc(outline)
            if labels and same_pages:
                doc.set_page_labels(labels)
            if meta:
                doc.set_metadata(meta)
            if doc.can_save_incrementally():
                doc.saveIncr()
            else:
                rewrite = output.with_suffix(".outline.tmp.pdf")
                doc.save(rewrite)
        finally:
            doc.close()
        if rewrite is not None:
            rewrite.replace(output)
    except Exception as exc:
        print(f"could not copy bookmarks from {src.name}: {exc}", file=sys.stderr)


def _write_image_pdf(
    tmp: Path,
    prepared: list[tuple[PagePlan, Path, str]],
    progress: ProgressCallback | None,
    cancel: CancelFlag | None,
) -> None:
    """One full-size JPEG per page, embedded untouched; the MediaBox shows only the crop box."""
    total_objects = 2 + len(prepared) * 3
    page_obj_nums = [5 + i * 3 for i in range(len(prepared))]
    with tmp.open("wb") as handle:
        offsets = [0]

        def write_obj(num: int, body: bytes) -> None:
            offsets.append(handle.tell())
            handle.write(f"{num} 0 obj\n".encode("ascii"))
            handle.write(body)
            if not body.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(b"endobj\n")

        handle.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        kids = " ".join(f"{num} 0 R" for num in page_obj_nums)
        write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        write_obj(2, f"<< /Type /Pages /Count {len(prepared)} /Kids [{kids}] >>".encode("ascii"))

        for i, (page, jpeg_path, mode) in enumerate(prepared):
            check_cancelled(cancel)
            left, top, right, bottom = page.crop
            scale = A4_HEIGHT_PT / page.height
            page_w = (right - left) * scale
            page_h = (bottom - top) * scale
            full_w = page.width * scale
            full_h = page.height * scale
            tx = -left * scale
            ty = -(page.height - bottom) * scale
            image_obj = 3 + i * 3
            content_obj = 4 + i * 3
            page_obj = 5 + i * 3
            image_bytes = jpeg_path.read_bytes()
            cs, decode = color_space(mode)
            image_dict = (
                f"<< /Type /XObject /Subtype /Image /Width {page.width} /Height {page.height} "
                f"/ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode{decode} "
                f"/Length {len(image_bytes)} >>\nstream\n"
            ).encode("ascii")
            write_obj(image_obj, image_dict + image_bytes + b"\nendstream")
            stream = (
                "q\n"
                f"{pdf_num(full_w)} 0 0 {pdf_num(full_h)} "
                f"{pdf_num(tx)} {pdf_num(ty)} cm\n"
                "/Im0 Do\n"
                "Q\n"
            ).encode("ascii")
            content = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"
            write_obj(content_obj, content)
            page_dict = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pdf_num(page_w)} {pdf_num(page_h)}] "
                f"/Resources << /XObject << /Im0 {image_obj} 0 R >> >> "
                f"/Contents {content_obj} 0 R >>"
            ).encode("ascii")
            write_obj(page_obj, page_dict)
            emit(progress, f"写入第 {page.page} 页", i + 1, len(prepared))

        xref_start = handle.tell()
        handle.write(f"xref\n0 {total_objects + 1}\n".encode("ascii"))
        handle.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            handle.write(f"{offset:010d} 00000 n \n".encode("ascii"))
        handle.write(
            (
                f"trailer\n<< /Size {total_objects + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_start}\n%%EOF\n"
            ).encode("ascii")
        )


TESSERACT_CANDIDATES = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
)


def find_tesseract() -> Path | None:
    which = shutil.which("tesseract")
    if which:
        return Path(which)
    for candidate in TESSERACT_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def tessdata_dir() -> Path | None:
    here = Path(__file__).resolve().parent
    bundled = here / "tessdata"
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).resolve().parent / "tessdata"
    if any((bundled / name).is_file() for name in ("chi_tra.traineddata", "chi_sim.traineddata", "eng.traineddata", "jpn.traineddata")):
        return bundled
    system = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
    if system.is_dir():
        return system
    return None


def ocr_available() -> bool:
    exe = find_tesseract()
    data = tessdata_dir()
    if exe is None or data is None:
        return False
    return any(
        (data / name).is_file()
        for name in ("chi_tra.traineddata", "chi_sim.traineddata", "eng.traineddata", "jpn.traineddata")
    )


def ocr_missing_reason() -> str:
    if find_tesseract() is None:
        return "未找到 Tesseract。请安装 Tesseract-OCR，或把它的安装目录加入 PATH。"
    data = tessdata_dir()
    if data is None or not any(
        (data / name).is_file()
        for name in ("chi_tra.traineddata", "chi_sim.traineddata", "eng.traineddata", "jpn.traineddata")
    ):
        return "未找到 OCR 字库（繁中 / 简中 / 英文 / 日文）。"
    return ""


def _hidden_run(cmd: list[str], data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, object] = {
        "check": False,
        "input": data,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo
    return subprocess.run(cmd, **kwargs)


def _run_tesseract(
    image: Path,
    lang: str,
    extra: list[str],
    user_words: Path | None = None,
    psm: int = 4,
) -> str:
    exe = find_tesseract()
    data = tessdata_dir()
    if exe is None:
        raise RuntimeError(ocr_missing_reason())
    # The image goes in through stdin because Tesseract cannot open paths longer
    # than 260 characters; its output is UTF-8 whatever the Windows code page is.
    cmd = [str(exe), "stdin", "stdout", "-l", lang, "--psm", str(psm), "--oem", "1"]
    if data is not None:
        cmd.extend(["--tessdata-dir", str(data)])
    if user_words is not None and user_words.is_file():
        cmd.extend(["--user-words", str(user_words)])
    for item in extra:
        if item == "tsv":
            cmd.extend(["-c", "tessedit_create_tsv=1"])
        else:
            cmd.append(item)
    proc = _hidden_run(cmd, image.read_bytes())
    stdout = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip() or stdout.strip()
        raise RuntimeError(detail or "tesseract failed")
    return stdout


def resolve_ocr_lang(requested: str = "chi_tra+chi_sim+eng+jpn") -> str:
    data = tessdata_dir()
    wanted = [part.strip() for part in requested.split("+") if part.strip()]
    if data is None:
        return "+".join(wanted) or "eng"
    present = [name for name in wanted if (data / f"{name}.traineddata").is_file()]
    if present:
        return "+".join(present)
    for fallback in ("chi_tra", "chi_sim", "jpn", "eng"):
        if (data / f"{fallback}.traineddata").is_file():
            return fallback
    return requested


def find_text_companion(src: Path) -> Path | None:
    stems = [src.stem]
    for tail in ("_裁边", "_内容修边", "_ocr"):
        if src.stem.endswith(tail):
            stems.append(src.stem[: -len(tail)])
    for stem in stems:
        for suffix in ("_LaTeX.pdf", "_latex.pdf", "_文字.pdf"):
            candidate = src.with_name(stem + suffix)
            if candidate.is_file():
                return candidate
    return None


def build_user_words(ref_pdf: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    words: set[str] = set()
    if HAS_FITZ:
        with fitz.open(ref_pdf) as doc:
            blob = "\n".join(page.get_text("text") or "" for page in doc)
    else:
        blob = ""
    for match in re.finditer(r"[\u3400-\u9fff]{2,12}", blob):
        words.add(match.group(0))
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9\-]{1,24}", blob):
        words.add(match.group(0))
    dest.write_text("\n".join(sorted(words)), encoding="utf-8")
    return dest


def prepare_ocr_image(image: Image.Image) -> Image.Image:
    from PIL import ImageOps

    gray = image.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=0.8)
    width, height = gray.size
    shortest = min(width, height)
    if shortest < 1800:
        scale = 1800 / max(shortest, 1)
        gray = gray.resize(
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            Image.Resampling.LANCZOS,
        )
    return gray.convert("RGB")


def _ocr_passes(requested: str) -> list[str]:
    parts = {part.strip() for part in requested.split("+") if part.strip()}
    data = tessdata_dir()

    def has(name: str) -> bool:
        return data is not None and (data / f"{name}.traineddata").is_file()

    passes: list[str] = []
    if "chi_tra" in parts and has("chi_tra"):
        passes.append("chi_tra+eng" if "eng" in parts and has("eng") else "chi_tra")
    if "chi_sim" in parts and has("chi_sim"):
        passes.append("chi_sim+eng" if "eng" in parts and has("eng") else "chi_sim")
    if "jpn" in parts and has("jpn"):
        passes.append("jpn+eng" if "eng" in parts and has("eng") else "jpn")
    if not passes and "eng" in parts and has("eng"):
        passes.append("eng")
    if not passes:
        resolved = resolve_ocr_lang(requested)
        passes.append(resolved)
    return passes


def _ocr_score(text: str, mean_conf: float, pass_lang: str = "") -> float:
    """Rank per-script passes: confidence, plus kana only when the jpn pass really reads Japanese."""
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    kana = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff")
    score = mean_conf + min(cjk, 400) * 0.02
    if pass_lang.startswith("jpn"):
        # Chinese read by the jpn model yields a few stray kana; real Japanese is kana-heavy.
        if kana >= 6 and kana / max(1, cjk + kana) >= 0.12:
            score += min(kana, 400) * 0.05
        else:
            score -= 5.0
    return score


def _tsv_mean_conf(tsv: str) -> float:
    confs: list[float] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            if int(parts[0]) == 5:
                confs.append(float(parts[10]))
        except ValueError:
            continue
    return float(sum(confs) / len(confs)) if confs else 0.0


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3000 <= code <= 0x30FF  # CJK punctuation, hiragana, katakana
        or 0x3400 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF  # full-width forms
    )


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def _tight(text: str, word: str) -> bool:
    """No space inside CJK text, as Tesseract's CJK models print it: 真誠,審判 but 2. 文章."""
    left, right = text[-1], word[0]
    if _is_cjk(left) and (_is_cjk(right) or _is_punct(right)):
        return True
    # Punctuation that closes a CJK run also sticks to the next CJK word.
    return _is_cjk(right) and _is_punct(left) and len(text) > 1 and _is_cjk(text[-2])


def _join_words(words: list[str]) -> str:
    """Join OCR words the way Tesseract's CJK models print them."""
    text = ""
    for word in words:
        if text and not _tight(text, word):
            text += " "
        text += word
    return text


def tsv_to_text(tsv: str) -> str:
    """Tesseract's plain text rebuilt from its TSV, so one run gives both."""
    lines: list[str] = []
    words: list[str] = []
    line_key: tuple[str, ...] | None = None
    para_key: tuple[str, ...] | None = None
    for row in tsv.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        word = parts[11].strip()
        if not word:
            continue
        this_para, this_line = tuple(parts[1:4]), tuple(parts[1:5])
        if this_line != line_key:
            if words:
                lines.append(_join_words(words))
                words = []
            if para_key is not None and this_para != para_key:
                lines.append("")
            line_key, para_key = this_line, this_para
        words.append(word)
    if words:
        lines.append(_join_words(words))
    return "\n".join(lines).strip()


def ocr_all_passes(
    image: Path,
    passes: list[str],
    user_words: Path | None = None,
) -> list[tuple[float, str, str, str]]:
    """(score, pass, text, tsv) for every per-script pass, best first; one Tesseract run each."""
    results = []
    for pass_lang in passes:
        tsv = _run_tesseract(image, pass_lang, ["tsv"], user_words=user_words, psm=4)
        text = tsv_to_text(tsv)
        results.append((_ocr_score(text, _tsv_mean_conf(tsv), pass_lang), pass_lang, text, tsv))
    results.sort(key=lambda item: item[0], reverse=True)
    return results


def ocr_image_bundle(
    image: Path,
    lang: str = "chi_tra+chi_sim+eng+jpn",
    user_words: Path | None = None,
    passes: list[str] | None = None,
) -> tuple[str, str]:
    """Return (plain_text, tsv) from the best per-script pass, so scripts do not fight."""
    _score, _pass, text, tsv = ocr_all_passes(image, passes or _ocr_passes(lang), user_words)[0]
    return text, tsv


OCR_SAMPLE_PAGES = 6


def _sample_indices(count: int, samples: int = OCR_SAMPLE_PAGES) -> list[int]:
    """Evenly spread page indices, skipping covers and front matter in longer books."""
    if count <= samples:
        return list(range(count))
    lo, hi = int(count * 0.1), int(count * 0.9)
    if hi - lo < samples:
        lo, hi = 0, count
    step = (hi - lo) / samples
    return sorted({min(hi - 1, lo + int(step * (i + 0.5))) for i in range(samples)})


def choose_ocr_passes(
    count: int,
    image_for: Callable[[int], Path],
    lang: str = "chi_tra+chi_sim+eng+jpn",
    user_words: Path | None = None,
    jobs: int = 1,
    cancel: CancelFlag | None = None,
) -> tuple[list[str], dict[int, tuple[str, str]]]:
    """Pick the book's scripts from a few sample pages.

    Every pass runs on the samples; the other pages run only the passes that won
    a sample, so a single-script book costs one Tesseract run per page. Returns
    (passes, {sample index: (text, tsv)}) so samples are not OCR'd twice.
    """
    passes = _ocr_passes(lang)
    if len(passes) <= 1 or count == 0:
        return passes, {}
    wins: Counter[str] = Counter()
    done: dict[int, tuple[str, str]] = {}

    def run(index: int) -> tuple[int, list[tuple[float, str, str, str]]]:
        check_cancelled(cancel)
        return index, ocr_all_passes(image_for(index), passes, user_words)

    with ThreadPoolExecutor(max_workers=_ocr_worker_count(jobs)) as pool:
        for index, results in pool.map(run, _sample_indices(count)):
            _score, best_pass, text, tsv = results[0]
            done[index] = (text, tsv)
            if text.strip():
                wins[best_pass] += 1
    return [name for name in passes if wins[name]] or passes, done


def prepare_plan_ocr_image(plan: PagePlan, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(plan.path) as im:
        rgb = im if im.mode == "RGB" else im.convert("RGB")
        prepared = prepare_ocr_image(rgb.crop(plan.crop))
        prepared.save(dest, format="JPEG", quality=95, subsampling=0)
    return dest


def ocr_plan_image(
    plan: PagePlan,
    dest: Path,
    lang: str = "chi_tra+chi_sim+eng+jpn",
    user_words: Path | None = None,
    passes: list[str] | None = None,
) -> tuple[str, Path, str]:
    prepare_plan_ocr_image(plan, dest)
    text, tsv = ocr_image_bundle(dest, lang, user_words=user_words, passes=passes)
    return text, dest, tsv


def _parse_tsv(tsv: str) -> list[tuple[int, int, int, int, str]]:
    words: list[tuple[int, int, int, int, str]] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            level = int(parts[0])
            conf = float(parts[10])
        except ValueError:
            continue
        if level != 5 or conf < 35:
            continue
        text = parts[11].strip()
        if not text:
            continue
        left, top, width, height = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]))
        if width <= 1 or height <= 1:
            continue
        words.append((left, top, width, height, text))
    return words


def _ocr_worker_count(jobs: int) -> int:
    if jobs <= 0:
        jobs = default_jobs()
    return max(1, min(int(jobs), 8))


def _ocr_text_font():
    if not HAS_FITZ:
        return None
    fonts = Path(r"C:\Windows\Fonts")
    for name in ("msyh.ttc", "msjh.ttc", "simsun.ttc", "msyh.ttf"):
        path = fonts / name
        if path.is_file():
            return fitz.Font(fontfile=str(path))
    return None


def _stamp_ocr_words(
    page: object,
    words: list[tuple[int, int, int, int, str]],
    img_w: int,
    img_h: int,
    font: object | None = None,
) -> int:
    """Write invisible, copyable OCR words using a CJK-capable font."""
    page_w, page_h = page.rect.width, page.rect.height
    scale_x = page_w / max(img_w, 1)
    scale_y = page_h / max(img_h, 1)
    written = 0
    if font is not None:
        writer = fitz.TextWriter(page.rect)
        for left, top, width, height, word in words:
            x0 = left * scale_x
            y0 = top * scale_y
            x1 = (left + width) * scale_x
            y1 = (top + height) * scale_y
            if x1 <= x0 or y1 <= y0:
                continue
            fontsize = max(4.0, (y1 - y0) * 0.88)
            writer.append(fitz.Point(x0, y1 - (y1 - y0) * 0.12), word, font=font, fontsize=fontsize)
            written += 1
        if written:
            writer.write_text(page, render_mode=3, overlay=True)
        return written
    for left, top, width, height, word in words:
        rect = fitz.Rect(
            left * scale_x,
            top * scale_y,
            (left + width) * scale_x,
            (top + height) * scale_y,
        )
        if rect.get_area() <= 0.5:
            continue
        fontsize = max(4.0, rect.height * 0.88)
        try:
            leftover = page.insert_textbox(
                rect,
                word,
                fontname="china-s",
                fontsize=fontsize,
                render_mode=3,
                overlay=True,
            )
            if leftover < 0:
                page.insert_text(
                    (rect.x0, rect.y1),
                    word,
                    fontname="china-s",
                    fontsize=max(3.0, fontsize * 0.85),
                    render_mode=3,
                    overlay=True,
                )
            written += 1
        except Exception:
            try:
                page.insert_text(
                    (rect.x0, rect.y1),
                    word,
                    fontname="china-s",
                    fontsize=fontsize,
                    render_mode=3,
                    overlay=True,
                )
                written += 1
            except Exception:
                continue
    return written


def _stamp_plain_text(page: object, text: str, font: object | None = None) -> int:
    """Fallback: put page text on the page so copy still works if TSV is empty."""
    body = " ".join(text.split())
    if not body:
        return 0
    rect = page.rect + (8, 8, -8, -8)
    if rect.is_empty:
        rect = page.rect
    if font is not None:
        writer = fitz.TextWriter(page.rect)
        writer.fill_textbox(rect, body, font=font, fontsize=7.0)
        writer.write_text(page, render_mode=3, overlay=True)
        return 1
    try:
        page.insert_textbox(rect, body, fontname="china-s", fontsize=7.0, render_mode=3, overlay=True)
        return 1
    except Exception:
        return 0


def _count_text_pages(pdf_path: Path) -> int:
    doc = fitz.open(pdf_path)
    try:
        return sum(1 for page in doc if (page.get_text("text") or "").strip())
    finally:
        doc.close()


def _require_ocr() -> None:
    if not HAS_FITZ:
        raise RuntimeError("OCR 需要 PyMuPDF")
    if not ocr_available():
        raise RuntimeError(ocr_missing_reason())


def _ocr_words_file(ref_pdf: Path | None, tmp_dir: Path, progress: ProgressCallback | None) -> Path | None:
    if ref_pdf is None or not ref_pdf.is_file():
        return None
    emit(progress, f"读取对照文本 {ref_pdf.name}", 0, 1)
    return build_user_words(ref_pdf, tmp_dir / "user-words.txt")


def _ocr_pages(
    count: int,
    image_for: Callable[[int], Path],
    labels: list[int],
    lang: str,
    user_words: Path | None,
    progress: ProgressCallback | None,
    cancel: CancelFlag | None,
    jobs: int,
) -> tuple[list[str], list[str]]:
    """OCR every page image: sample pages pick the scripts, the rest run only those passes."""
    emit(progress, "OCR 判断文种", 0, count)
    passes, done = choose_ocr_passes(count, image_for, lang, user_words, jobs, cancel)
    emit(progress, f"OCR 使用 {' / '.join(passes)}", len(done), count)
    texts = [""] * count
    tsvs = [""] * count
    for index, (text, tsv) in done.items():
        texts[index], tsvs[index] = text, tsv
    finished = len(done)

    def run(index: int) -> tuple[int, str, str]:
        check_cancelled(cancel)
        text, tsv = ocr_image_bundle(image_for(index), lang, user_words, passes)
        return index, text, tsv

    with ThreadPoolExecutor(max_workers=_ocr_worker_count(jobs)) as pool:
        futures = [pool.submit(run, index) for index in range(count) if index not in done]
        try:
            for future in as_completed(futures):
                index, text, tsv = future.result()
                texts[index], tsvs[index] = text, tsv
                finished += 1
                emit(progress, f"OCR 第 {labels[index]} 页", finished, count)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return texts, tsvs


def _write_text_layer(
    output: Path,
    images: list[Path],
    texts: list[str],
    tsvs: list[str],
    labels: list[int],
    progress: ProgressCallback | None,
    write_txt: bool,
) -> Path | None:
    """Stamp invisible OCR words on each page; optionally write the .txt transcript."""
    emit(progress, "写入可复制文字层", 0, 1)
    font = _ocr_text_font()
    stamped = output.with_name(output.stem + "_ocr.tmp.pdf")
    doc = fitz.open(output)
    try:
        for index, image in enumerate(images):
            if index >= doc.page_count:
                break
            with Image.open(image) as im:
                img_w, img_h = im.size
            if _stamp_ocr_words(doc[index], _parse_tsv(tsvs[index]), img_w, img_h, font) == 0:
                _stamp_plain_text(doc[index], texts[index], font)
        try:
            # Embed only the glyphs used; the whole Microsoft YaHei adds ~19 MB per PDF.
            doc.subset_fonts()
        except Exception as exc:
            print(f"font subsetting skipped: {exc}", file=sys.stderr)
        doc.save(stamped, deflate=True, garbage=4)
    finally:
        doc.close()
    text_pages = _count_text_pages(stamped)
    if text_pages == 0:
        stamped.unlink(missing_ok=True)
        raise RuntimeError("OCR 已跑完，但没有写出可复制文字。请确认已安装 Tesseract，并重试「OCR 全书」。")
    emit(progress, f"已写入 {text_pages} 页可复制文字", text_pages, len(images))
    stamped.replace(output)
    if not write_txt:
        return None
    text_path = output.with_name(output.stem + ".txt")
    chunks = [f"# 第 {label} 页\n{text.strip()}\n" for label, text in zip(labels, texts)]
    text_path.write_text("\n".join(chunks).strip() + "\n", encoding="utf-8")
    return text_path


def apply_ocr_to_pdf(
    output: Path,
    plans: list[PagePlan],
    lang: str = "chi_tra+chi_sim+eng+jpn",
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
    jobs: int = 1,
    write_txt: bool = False,
    ref_pdf: Path | None = None,
) -> Path | None:
    _require_ocr()
    tmp_dir = output.with_name(output.stem + "_ocr_tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        words_file = _ocr_words_file(ref_pdf, tmp_dir, progress)
        images = [tmp_dir / f"ocr-{plan.page:04d}.jpg" for plan in plans]

        def image_for(index: int) -> Path:
            if not images[index].is_file():
                prepare_plan_ocr_image(plans[index], images[index])
            return images[index]

        labels = [plan.page for plan in plans]
        texts, tsvs = _ocr_pages(len(plans), image_for, labels, lang, words_file, progress, cancel, jobs)
        return _write_text_layer(output, images, texts, tsvs, labels, progress, write_txt)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def apply_ocr_to_existing_pdf(
    output: Path,
    lang: str = "chi_tra+chi_sim+eng+jpn",
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
    jobs: int = 8,
    write_txt: bool = False,
    ref_pdf: Path | None = None,
    dpi: int = 220,
) -> Path | None:
    """Add a copyable text layer to an already written image-only PDF."""
    _require_ocr()
    if not output.is_file():
        raise RuntimeError(f"找不到 PDF：{output}")
    tmp_dir = output.with_name(output.stem + "_ocr_tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        companion = ref_pdf if ref_pdf is not None else find_text_companion(output)
        words_file = _ocr_words_file(companion, tmp_dir, progress)
        images: list[Path] = []
        doc = fitz.open(output)
        try:
            zoom = max(1.5, min(4.0, float(dpi) / 72.0))
            matrix = fitz.Matrix(zoom, zoom)
            for index in range(doc.page_count):
                check_cancelled(cancel)
                dest = tmp_dir / f"ocr-{index + 1:04d}.jpg"
                pix = doc[index].get_pixmap(matrix=matrix, alpha=False)
                pix.save(dest.as_posix(), jpg_quality=95)
                images.append(dest)
                emit(progress, f"抽出第 {index + 1} 页", index + 1, doc.page_count)
        finally:
            doc.close()
        labels = list(range(1, len(images) + 1))
        texts, tsvs = _ocr_pages(
            len(images), images.__getitem__, labels, lang, words_file, progress, cancel, jobs
        )
        return _write_text_layer(output, images, texts, tsvs, labels, progress, write_txt)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def resolve_path(path: str | Path, cwd: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else cwd / p


def parse_page_spec(spec: str) -> list[int]:
    pages: set[int] = set()
    if not spec.strip():
        return []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            if not start_s.strip() or not end_s.strip():
                raise ValueError(f"Invalid page range {token!r}")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise ValueError(f"Invalid page range {token!r}") from exc
            if start > end:
                start, end = end, start
            pages.update(range(start, end + 1))
        else:
            try:
                pages.add(int(token))
            except ValueError as exc:
                raise ValueError(f"Invalid page number {token!r}") from exc
    return sorted(p for p in pages if p > 0)


def validate_pages_in_pdf(pages: list[int], page_total: int, option_name: str) -> None:
    invalid = [page for page in pages if page > page_total]
    if invalid:
        shown = ", ".join(str(page) for page in invalid[:12])
        if len(invalid) > 12:
            shown += ", ..."
        raise ValueError(f"{option_name} contains page(s) beyond the {page_total}-page PDF: {shown}")


def clamp_crop(crop: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = crop
    left = max(0, min(width - 1, left))
    top = max(0, min(height - 1, top))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    return left, top, right, bottom


def parse_page_crop_spec(specs: list[str]) -> dict[int, tuple[int, int, int, int]]:
    crops: dict[int, tuple[int, int, int, int]] = {}
    for spec in specs:
        for token in spec.split(";"):
            token = token.strip()
            if not token:
                continue
            if ":" not in token:
                raise ValueError(f"Manual crop must look like PAGE:LEFT,TOP,RIGHT,BOTTOM, got {token!r}")
            page_s, crop_s = token.split(":", 1)
            page = int(page_s.strip())
            values = [int(part) for part in re.split(r"[,\s]+", crop_s.strip()) if part]
            if len(values) != 4:
                raise ValueError(f"Manual crop for page {page} needs four numbers, got {crop_s!r}")
            if page <= 0:
                raise ValueError("Manual crop page numbers must be positive")
            left, top, right, bottom = values
            if right <= left or bottom <= top:
                raise ValueError(f"Manual crop for page {page} has an empty rectangle")
            crops[page] = (left, top, right, bottom)
    return crops


def parse_page_rotate_spec(specs: list[str]) -> dict[int, float]:
    rotates: dict[int, float] = {}
    for spec in specs:
        for token in spec.split(";"):
            token = token.strip()
            if not token:
                continue
            if ":" not in token:
                raise ValueError(f"Page rotate must look like PAGE:DEGREES, got {token!r}")
            page_s, angle_s = token.split(":", 1)
            page = int(page_s.strip())
            if page <= 0:
                raise ValueError("Rotate page numbers must be positive")
            rotates[page] = float(angle_s.strip())
    return rotates


def render_inspection_pages_fitz(
    pdf: Path,
    pages: list[int],
    out_dir: Path,
    dpi: int,
    output_page_numbers: list[int] | None = None,
) -> None:
    if not HAS_FITZ:
        raise RuntimeError("PyMuPDF is not installed")
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = output_page_numbers or pages
    with fitz.open(pdf) as doc:
        for page, label in zip(pages, labels):
            pix = doc[page - 1].get_pixmap(dpi=dpi, alpha=False)
            dest = out_dir / f"{pdf.stem}_p{label:03d}.png"
            pix.save(dest.as_posix())


def render_inspection_pages(
    pdf: Path,
    pages: list[int],
    out_dir: Path,
    cwd: Path,
    dpi: int,
    output_page_numbers: list[int] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = output_page_numbers or pages
    if HAS_FITZ:
        render_inspection_pages_fitz(pdf, pages, out_dir, dpi, labels)
        return
    for page, label in zip(pages, labels):
        prefix = out_dir / f"{pdf.stem}_p{label:03d}"
        run(
            [
                "pdftoppm",
                "-png",
                "-singlefile",
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                str(dpi),
                str(pdf),
                str(prefix),
            ],
            cwd,
        )


def execute_job(
    job: CropJob,
    progress: ProgressCallback | None = None,
    cancel: CancelFlag | None = None,
) -> list[PagePlan]:
    src = job.src
    output = job.output
    work = job.work
    cwd = src.parent
    if not src.is_file():
        raise RuntimeError(f"Input PDF not found: {src}")
    if src.resolve() == output.resolve():
        raise RuntimeError("Output PDF must be different from the input PDF")
    emit(progress, "读取页数", 0, 0)
    count = page_count(src, cwd, job.backend)
    selected = list(job.pages) if job.pages else list(range(1, count + 1))
    validate_pages_in_pdf(selected, count, "pages")
    validate_pages_in_pdf(sorted(job.manual_crops), count, "page-crop")
    validate_pages_in_pdf(sorted(job.page_rotates), count, "page-rotate")
    inspect_pages = [page for page in job.inspect_pages if page in set(selected)]
    validate_pages_in_pdf(job.inspect_pages, count, "inspect-pages")
    emit(progress, f"共 {count} 页，准备抽图", 0, count)
    images = extract_images(
        src,
        work,
        cwd,
        reuse=job.reuse,
        expected_pages=count,
        pages=selected,
        render_dpi=job.render_dpi,
        progress=progress,
        cancel=cancel,
        backend=job.backend,
    )
    check_cancelled(cancel)
    emit(progress, "开始分析裁切框", 0, len(images))
    plans = analyze_pages(
        images,
        job.min_width_ratio,
        job.min_height_ratio,
        job.manual_crops,
        job.min_full_width_ratio,
        job.min_full_height_ratio,
        job.page_rotates,
        progress,
        cancel,
        job.jobs,
        job.crop_mode,
        job.auto_rotate,
    )
    if job.report is not None:
        write_report(plans, job.report)
    if job.write_pdf:
        emit(progress, "写入裁边 PDF", 0, len(plans))
        write_pdf_from_plans(plans, output, progress, cancel, source=src)
        if job.ocr:
            emit(progress, "开始 OCR", 0, len(plans))
            apply_ocr_to_pdf(
                output,
                plans,
                job.ocr_lang,
                progress,
                cancel,
                job.jobs,
                write_txt=job.ocr_txt,
                ref_pdf=job.ocr_ref or find_text_companion(job.src),
            )
    if inspect_pages and job.write_pdf and job.inspect_dir is not None:
        emit(progress, "渲染检查图", 0, len(inspect_pages))
        output_index = {plan.page: i + 1 for i, plan in enumerate(plans)}
        pdf_pages = [output_index[page] for page in inspect_pages if page in output_index]
        labels = [page for page in inspect_pages if page in output_index]
        render_inspection_pages(output, pdf_pages, job.inspect_dir, cwd, job.inspect_dpi, labels)
    emit(progress, "完成", len(plans), len(plans))
    return plans


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crop dirty PDF scan edges while keeping sparse pages close to the normal page size."
    )
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT, help="source PDF")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="cropped PDF path")
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR, help="directory for extracted page images")
    parser.add_argument("--report", default=DEFAULT_REPORT, help="CSV crop report path")
    parser.add_argument(
        "--reuse-images",
        action="store_true",
        help="reuse exact page-NNN.jpg cache files only when their source metadata matches this PDF",
    )
    parser.add_argument(
        "--min-page-ratio",
        type=float,
        default=0.86,
        help="minimum width and height ratio for sparse pages after edge trimming",
    )
    parser.add_argument("--min-width-ratio", type=float, help="override minimum sparse-page width ratio")
    parser.add_argument("--min-height-ratio", type=float, help="override minimum sparse-page height ratio")
    parser.add_argument(
        "--min-full-page-ratio",
        type=float,
        default=0.90,
        help=(
            "minimum width and height ratio relative to the original scan after edge trimming; "
            "guards sparse pages from over-cropping"
        ),
    )
    parser.add_argument("--min-full-width-ratio", type=float, help="override minimum original-scan width ratio")
    parser.add_argument("--min-full-height-ratio", type=float, help="override minimum original-scan height ratio")
    parser.add_argument(
        "--page-crop",
        action="append",
        default=[],
        metavar="PAGE:L,T,R,B",
        help=(
            "manual pixel crop override; repeat or separate entries with semicolons, "
            "e.g. --page-crop 266:8,0,1095,1636"
        ),
    )
    parser.add_argument(
        "--page-rotate",
        action="append",
        default=[],
        metavar="PAGE:DEG",
        help="rotate a page before cropping, Pillow degrees (positive = counterclockwise), e.g. --page-rotate 1:1.0",
    )
    parser.add_argument(
        "--pages",
        default="",
        help="optional subset of pages to process, e.g. 1,2,10-12",
    )
    parser.add_argument(
        "--inspect-pages",
        default="",
        help="optional pages to render for review, e.g. 56,177,185 or 50-60",
    )
    parser.add_argument("--inspect-dir", default=DEFAULT_INSPECT_DIR, help="directory for rendered review PNGs")
    parser.add_argument("--inspect-dpi", type=int, default=120, help="DPI for review PNGs")
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="parallel workers for crop analysis (0 = default 14, capped at the CPU thread count; 1 = sequential)",
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        default=DEFAULT_RENDER_DPI,
        help="DPI used when a page has no extractable full-page JPEG and must be rasterized",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "pymupdf", "poppler"),
        default="auto",
        help="PDF image backend; auto prefers PyMuPDF so the program can run without Poppler",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="extract and write the crop report without generating the output PDF",
    )
    parser.add_argument(
        "--crop-mode",
        choices=("edges", "content"),
        default="edges",
        help="edges = only trim dirty scan borders and keep paper margins; content = crop tightly around text",
    )
    parser.add_argument(
        "--auto-rotate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="automatically deskew tilted spines and lightly skewed text pages",
    )
    parser.add_argument(
        "--ocr",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="run Tesseract OCR and write a searchable PDF",
    )
    parser.add_argument(
        "--ocr-txt",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="also write a sidecar .txt transcript when OCR is on",
    )
    parser.add_argument(
        "--ocr-lang",
        default="chi_tra+chi_sim+eng+jpn",
        help="Tesseract languages; default traditional/simplified Chinese + English + Japanese",
    )
    parser.add_argument(
        "--ocr-existing",
        metavar="PDF",
        help="add a copyable text layer to an already cropped image-only PDF",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    min_width_ratio = args.min_width_ratio if args.min_width_ratio is not None else args.min_page_ratio
    min_height_ratio = args.min_height_ratio if args.min_height_ratio is not None else args.min_page_ratio
    min_full_width_ratio = (
        args.min_full_width_ratio if args.min_full_width_ratio is not None else args.min_full_page_ratio
    )
    min_full_height_ratio = (
        args.min_full_height_ratio if args.min_full_height_ratio is not None else args.min_full_page_ratio
    )
    for name, value in [
        ("min-width-ratio", min_width_ratio),
        ("min-height-ratio", min_height_ratio),
        ("min-full-width-ratio", min_full_width_ratio),
        ("min-full-height-ratio", min_full_height_ratio),
    ]:
        if not 0 < value <= 1:
            parser.error(f"--{name} must be greater than 0 and no more than 1")
    if args.render_dpi < 50 or args.render_dpi > 2400:
        parser.error("--render-dpi must be between 50 and 2400")
    if args.jobs < 0:
        parser.error("--jobs must be 0 or a positive integer")
    try:
        manual_crops = parse_page_crop_spec(args.page_crop)
        page_rotates = parse_page_rotate_spec(args.page_rotate)
        inspect_pages = parse_page_spec(args.inspect_pages)
        selected_pages = parse_page_spec(args.pages) or None
    except ValueError as exc:
        parser.error(str(exc))

    cwd = Path.cwd()
    if args.ocr_existing:
        existing = resolve_path(args.ocr_existing, cwd)
        if not existing.is_file():
            parser.error(f"PDF not found: {existing}")

        def progress(message: str, current: int, total: int) -> None:
            if total:
                print(f"{message} ({current}/{total})", flush=True)
            else:
                print(message, flush=True)

        apply_ocr_to_existing_pdf(
            existing,
            lang=args.ocr_lang,
            progress=progress,
            jobs=args.jobs,
            write_txt=args.ocr_txt,
            ref_pdf=find_text_companion(existing),
        )
        print(f"ocr wrote text layer: {existing}")
        return 0
    src = resolve_path(args.input, cwd)
    output = resolve_path(args.output, cwd)
    work = resolve_path(args.work_dir, cwd)
    report = resolve_path(args.report, cwd)
    job = CropJob(
        src=src,
        output=output,
        work=work,
        report=report,
        reuse=args.reuse_images,
        min_width_ratio=min_width_ratio,
        min_height_ratio=min_height_ratio,
        min_full_width_ratio=min_full_width_ratio,
        min_full_height_ratio=min_full_height_ratio,
        manual_crops=manual_crops,
        page_rotates=page_rotates,
        pages=selected_pages,
        inspect_pages=inspect_pages,
        inspect_dir=resolve_path(args.inspect_dir, cwd) if inspect_pages else None,
        inspect_dpi=args.inspect_dpi,
        jobs=args.jobs,
        render_dpi=args.render_dpi,
        write_pdf=not args.analyze_only,
        backend=args.backend,
        crop_mode=args.crop_mode,
        auto_rotate=args.auto_rotate,
        ocr=args.ocr,
        ocr_lang=args.ocr_lang,
        ocr_txt=args.ocr_txt,
        ocr_ref=find_text_companion(src),
    )
    try:
        plans = execute_job(job)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"wrote {output.name}" if job.write_pdf else f"analyzed {len(plans)} page(s)")
    print(f"pages: {len(plans)}")
    print(f"report: {report.name}")
    if inspect_pages and job.write_pdf:
        print(f"inspection pages: {', '.join(str(p) for p in inspect_pages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
