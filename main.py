#!/usr/bin/env python3
"""
QA Claude – streaming mode
อ่านโจทย์จากรูปภาพ ถาม Claude (streaming) และสร้าง PDF ผ่าน XeLaTeX
"""

import os
import sys
import shutil

import anthropic

from qa_common import (
    INPUT_DIR, DONE_DIR, OUTPUT_DIR,
    _IMAGE_EXTS,
    Usage, load_config,
    process_image, create_pdf,
    _fmt_elapsed, _print_usage_summary,
)

import time
from pathlib import Path


# ─── Per-image Processing ─────────────────────────────────────────────────────

def process_one(client: anthropic.Anthropic, img_path: Path, usage: Usage) -> None:
    """Process a single image: extract → answer → PDF, then move to done."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    output_pdf = OUTPUT_DIR / f"{img_path.stem}_answer.pdf"
    answer = process_image(client, str(img_path), usage)
    create_pdf(answer, str(output_pdf))

    shutil.move(str(img_path), DONE_DIR / img_path.name)
    print(f"📦  Moved  {img_path.name}  →  input_done/")


# ─── Batch Processing (streaming) ─────────────────────────────────────────────

def run_batch(client: anthropic.Anthropic) -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_images = sorted(f for f in INPUT_DIR.iterdir() if f.suffix.lower() in _IMAGE_EXTS)

    if not all_images:
        print("📂  input/ is empty — nothing to process.")
        return

    batch_size = int(load_config().get("batch_size", 5))
    batch = all_images[:batch_size]

    print(f"📋  Found {len(all_images)} image(s), processing {len(batch)} (batch_size={batch_size})")

    usage = Usage()
    success_count = failure_count = 0
    for img_path in batch:
        print(f"\n{'=' * 60}")
        print(f"🖼   {img_path.name}")
        try:
            process_one(client, img_path, usage)
            success_count += 1
        except Exception as exc:
            print(f"❌  Failed: {exc}")
            failure_count += 1

    print(f"\n{'=' * 60}")
    print(f"Done — {success_count} succeeded, {failure_count} failed.")
    remaining = len(all_images) - batch_size
    if remaining > 0:
        print(f"📂  {remaining} image(s) remaining in input/")
    _print_usage_summary(usage)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Add it to .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Single-file mode: python main.py <image> [output.pdf]
    if len(sys.argv) >= 2:
        image_path = Path(sys.argv[1])
        if not image_path.exists():
            print(f"Error: file not found – {image_path}")
            sys.exit(1)
        output_pdf = (
            Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(f"{image_path.stem}_answer.pdf")
        )
        print("🚀  QA Claude – single file mode")
        print("=" * 60)
        t_start = time.perf_counter()
        usage = Usage()
        try:
            answer = process_image(client, str(image_path), usage)
            create_pdf(answer, str(output_pdf))
        except RuntimeError as exc:
            print(f"\n❌  {exc}")
            sys.exit(1)
        _print_usage_summary(usage)
        print(f"\n✨  Done  →  {output_pdf.resolve()}  [{_fmt_elapsed(time.perf_counter() - t_start)}]")
        return

    # Batch mode: read from input/, output to output/, move done to input_done/
    print("🚀  QA Claude – batch mode (streaming)")
    print("=" * 60)
    run_batch(client)


if __name__ == "__main__":
    main()
