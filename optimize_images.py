"""
optimize_images.py
-------------------
Resizes and compresses the photos used on the IEEE FSM site (Images/,
Act_Images/, assets/team/). Phone-camera photos are usually 3000-4000px wide
and several MB each — way bigger than they're ever displayed on the page,
which is the #1 cause of slow loading when a site has "many pics".

What it does, per image:
  1. Resizes it down to MAX_WIDTH (default 1600px) if it's wider than that —
     that's already far more than any card/photo on the site displays at.
  2. Re-saves it as both a compressed .jpg (fallback) and a .webp (smaller,
     used first by browsers that support it) at QUALITY (default 78).
  3. Leaves the original file untouched, writing optimized copies into an
     "optimized/" folder next to the source, mirroring the folder structure.

Usage:
  pip install Pillow
  python optimize_images.py Images Act_Images assets/team

Then update your <img> tags to use a <picture> element pointing at the
webp version first, e.g.:

  <picture>
    <source srcset="optimized/Act_Images/IMG_0001.webp" type="image/webp">
    <img src="optimized/Act_Images/IMG_0001.jpg" loading="lazy" decoding="async" alt="...">
  </picture>

(Ask Claude to do this find-and-replace across index.html once you've run
the script and are happy with the output quality/size.)
"""

import os
import sys
from pathlib import Path

from PIL import Image, ImageOps

MAX_WIDTH = 1600      # px — plenty for anything on this page, phones shoot 3000-4000px
QUALITY = 78          # 70-85 is a good quality/size balance for photos
SKIP_DIRS = {"optimized", "node_modules", ".git"}
VALID_EXT = {".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"}


def optimize_one(src_path: Path, out_root: Path, in_root: Path):
    rel = src_path.relative_to(in_root)
    out_dir = out_root / rel.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)  # respect phone photo rotation
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        if img.width > MAX_WIDTH:
            new_height = int(img.height * (MAX_WIDTH / img.width))
            img = img.resize((MAX_WIDTH, new_height), Image.LANCZOS)

        stem = rel.stem
        jpg_path = out_dir / f"{stem}.jpg"
        webp_path = out_dir / f"{stem}.webp"

        img.save(jpg_path, "JPEG", quality=QUALITY, optimize=True, progressive=True)
        img.save(webp_path, "WEBP", quality=QUALITY)

    before = src_path.stat().st_size
    after = jpg_path.stat().st_size + webp_path.stat().st_size
    print(f"{rel}: {before/1024:.0f} KB -> {after/1024:.0f} KB (jpg+webp combined)")


def main(folders):
    if not folders:
        print("Usage: python optimize_images.py <folder1> [folder2] ...")
        sys.exit(1)

    for folder in folders:
        in_root = Path(folder)
        if not in_root.exists():
            print(f"Skipping {folder} (not found)")
            continue
        out_root = in_root / "optimized"

        for path in in_root.rglob("*"):
            if path.is_dir() or "optimized" in path.parts:
                continue
            if path.suffix not in VALID_EXT:
                continue
            try:
                optimize_one(path, out_root, in_root)
            except Exception as e:
                print(f"! failed on {path}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
