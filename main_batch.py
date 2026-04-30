#!/usr/bin/env python3
"""
QA Claude – Batch API mode
อ่านโจทย์จากรูปภาพ ส่งเป็น batch ถาม Claude และสร้าง PDF ผ่าน XeLaTeX
"""

import os
import re
import sys
import shutil
import time

import anthropic

from qa_common import (
    INPUT_DIR, DONE_DIR, OUTPUT_DIR,
    _IMAGE_EXTS,
    Usage, load_config,
    load_token_stats, save_token_stats, compute_max_tokens,
    image_to_base64, process_image, create_pdf,
    _find_xelatex, _fmt_elapsed, _print_usage_summary,
    _ANSWER_SYSTEM,
)

from pathlib import Path


# ─── Batch API Helpers ────────────────────────────────────────────────────────

def _sanitize_custom_id(name: str) -> str:
    """Strip characters not allowed by the Batch API custom_id pattern."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return sanitized[:64]


def _build_batch_request(image_path: Path, max_tokens: int) -> dict:
    image_b64_data, media_type = image_to_base64(str(image_path))
    return {
        "custom_id": _sanitize_custom_id(image_path.name),
        "params": {
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": _ANSWER_SYSTEM}],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64_data}},
                    {"type": "text", "text": "กรุณาตอบโจทย์จากรูปภาพนี้"},
                ],
            }],
        },
    }


def _submit_and_poll_batch(client: anthropic.Anthropic, requests: list[dict]) -> list:
    """Submit requests as a batch and poll until all complete."""
    batch = client.messages.batches.create(requests=requests)
    print(f"  Batch ID   : {batch.id}")
    print(f"  Status     : {batch.processing_status}")

    while batch.processing_status != "ended":
        time.sleep(30)
        batch = client.messages.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(
            f"  Polling... : {batch.processing_status}"
            f"  |  succeeded={counts.succeeded}"
            f"  errored={counts.errored}"
            f"  processing={counts.processing}"
        )

    return list(client.messages.batches.results(batch.id))


# ─── Batch Processing ─────────────────────────────────────────────────────────

def run_batch(client: anthropic.Anthropic) -> None:
    t_start = time.perf_counter()
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_images = sorted(f for f in INPUT_DIR.iterdir() if f.suffix.lower() in _IMAGE_EXTS)

    if not all_images:
        print("📂  input/ is empty — nothing to process.")
        return

    xelatex_path = _find_xelatex()
    if not xelatex_path:
        print("❌  xelatex not found — cannot create PDFs.")
        print("   Windows : install MiKTeX → https://miktex.org/")
        print("   Linux   : sudo apt install texlive-xetex texlive-lang-other")
        sys.exit(1)

    batch_size = int(load_config().get("batch_size", 5))
    batch_images = all_images[:batch_size]

    print(f"📋  Found {len(all_images)} image(s), processing {len(batch_images)} (batch_size={batch_size})")

    token_history = load_token_stats()
    max_tokens = compute_max_tokens(token_history)
    print(f"   max_tokens={max_tokens}  (history={len(token_history)} samples)")

    print("\n📤  Building batch requests...")
    requests: list[dict] = []
    img_map: dict[str, Path] = {}
    for img_path in batch_images:
        req = _build_batch_request(img_path, max_tokens)
        requests.append(req)
        img_map[req["custom_id"]] = img_path
        print(f"   + {img_path.name}")

    print(f"\n⏳  Submitting {len(requests)} request(s) to Batch API...")
    results = _submit_and_poll_batch(client, requests)

    usage = Usage(batch=True)
    success_count = 0
    failures: list[tuple[str, str]] = []
    new_token_samples: list[int] = []

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    for result in results:
        img_path = img_map.get(result.custom_id)
        name = img_path.name if img_path else result.custom_id
        print(f"\n{'=' * 60}")
        print(f"🖼   {name}")

        if result.result.type == "succeeded":
            message = result.result.message
            usage.add(message.usage)
            new_token_samples.append(message.usage.output_tokens)
            answer = message.content[0].text
            stem = Path(result.custom_id).stem
            output_pdf = OUTPUT_DIR / f"{stem}_answer.pdf"
            try:
                create_pdf(answer, str(output_pdf), xelatex_path=xelatex_path)
                if img_path:
                    shutil.move(str(img_path), DONE_DIR / img_path.name)
                    print(f"📦  Moved  {name}  →  input_done/")
                success_count += 1
            except Exception as exc:
                print(f"❌  PDF failed: {exc}")
                failures.append((name, str(exc)))
        else:
            reason = result.result.type
            if hasattr(result.result, "error"):
                reason = f"{reason}: {result.result.error}"
            print(f"❌  API error: {reason}")
            failures.append((name, reason))

    if new_token_samples:
        token_history.extend(new_token_samples)
        save_token_stats(token_history)

    print(f"\n{'=' * 60}")
    print(f"Done — {success_count} succeeded, {len(failures)} failed.  [{_fmt_elapsed(time.perf_counter() - t_start)}]")

    if failures:
        print(f"\n⚠️  Failed images ({len(failures)}):")
        for fname, reason in failures:
            print(f"   • {fname}")
            print(f"     {reason}")

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

    # Single-file mode: python main_batch.py <image> [output.pdf]
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
    print("🚀  QA Claude – batch mode (Batch API)")
    print("=" * 60)

    cfg = load_config()
    repetitions = int(cfg.get("number_of_repetitions", 1))
    wait_seconds = int(cfg.get("wait_time_between_repetitions", 60))

    for rep in range(repetitions):
        if repetitions > 1:
            print(f"\n🔁  รอบที่ {rep + 1} / {repetitions}")
            print("=" * 60)
        run_batch(client)
        if rep < repetitions - 1:
            print(f"\n⏱   รอ {wait_seconds} วินาทีก่อนรอบถัดไป...")
            time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
