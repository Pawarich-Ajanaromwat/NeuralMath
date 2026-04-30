#!/usr/bin/env python3
"""
QA Claude – Python Exam Mode
รับไฟล์ PDF โจทย์ Python ส่งถาม Claude แล้วสร้าง PDF เฉลยพร้อม Python Code
อธิบายละเอียดสำหรับเด็กมัธยม (ม.1–ม.6)

วิธีใช้:
  python main_python.py                    # batch mode (อ่านจาก input/)
  python main_python.py <exam.pdf>         # single file mode
  python main_python.py <exam.pdf> out.pdf
"""

import os
import re
import sys
import shutil
import time

import anthropic

from qa_common import (
    INPUT_DIR, DONE_DIR, OUTPUT_DIR,
    _PDF_EXTS,
    Usage, compute_max_tokens_python, load_config,
    load_token_stats, save_token_stats,
    STATS_PYTHON_FILE,
    pdf_to_base64, create_python_pdf, extract_python_code,
    _find_xelatex, _fmt_elapsed, _print_usage_summary,
    _PYTHON_ANSWER_SYSTEM, _PYTHON_THINKING_BUDGET,
)

from pathlib import Path


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sanitize_custom_id(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


def _build_batch_request(pdf_path: Path, max_tokens: int) -> dict:
    pdf_b64 = pdf_to_base64(str(pdf_path))
    return {
        "custom_id": _sanitize_custom_id(pdf_path.name),
        "params": {
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "thinking": {"type": "enabled", "budget_tokens": _PYTHON_THINKING_BUDGET},
            "system": [{"type": "text", "text": _PYTHON_ANSWER_SYSTEM}],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "document", "source": {
                        "type": "base64", "media_type": "application/pdf", "data": pdf_b64,
                    }},
                    {"type": "text", "text": (
                        "กรุณาแก้โจทย์ Python จาก PDF นี้ "
                        "พร้อมอธิบายโค้ดให้เด็กมัธยมเข้าใจ"
                    )},
                ],
            }],
        },
    }


def _submit_and_poll_batch(client: anthropic.Anthropic, requests: list[dict]) -> list:
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


# ─── Single-PDF Streaming ─────────────────────────────────────────────────────

def process_pdf_python(client: anthropic.Anthropic, pdf_path: str, usage: Usage) -> str:
    """Send Python exam PDF to Claude (streaming) and return structured answer."""
    token_history = load_token_stats(STATS_PYTHON_FILE)
    max_tokens = max(compute_max_tokens_python(token_history), _PYTHON_THINKING_BUDGET + 4000)

    print(f"\n📄  Processing: {pdf_path}\n" + "─" * 60)
    print(f"   max_tokens={max_tokens}  thinking_budget={_PYTHON_THINKING_BUDGET}  (history={len(token_history)} samples)")

    pdf_b64 = pdf_to_base64(pdf_path)
    chunks: list[str] = []

    t0 = time.perf_counter()
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        thinking={"type": "enabled", "budget_tokens": _PYTHON_THINKING_BUDGET},
        system=[{"type": "text", "text": _PYTHON_ANSWER_SYSTEM}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf", "data": pdf_b64,
                }},
                {"type": "text", "text": (
                    "กรุณาแก้โจทย์ Python จาก PDF นี้ "
                    "พร้อมอธิบายโค้ดให้เด็กมัธยมเข้าใจ"
                )},
            ],
        }],
    ) as stream:
        for chunk in stream.text_stream:
            chunks.append(chunk)
            print(chunk, end="", flush=True)

    msg = stream.get_final_message()
    usage.add(msg.usage)
    token_history.append(msg.usage.output_tokens)
    save_token_stats(token_history, STATS_PYTHON_FILE)

    print(f"\n   tokens: in={msg.usage.input_tokens} out={msg.usage.output_tokens}")
    print(f"   elapsed: {_fmt_elapsed(time.perf_counter() - t0)}")
    print("─" * 60)
    return "".join(chunks)


# ─── Batch Processing ─────────────────────────────────────────────────────────

def run_batch(client: anthropic.Anthropic) -> None:
    t_start = time.perf_counter()
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_pdfs = sorted(f for f in INPUT_DIR.iterdir() if f.suffix.lower() in _PDF_EXTS)
    if not all_pdfs:
        print("📂  input/ is empty — nothing to process.")
        return

    xelatex_path = _find_xelatex()
    if not xelatex_path:
        print("❌  xelatex not found — cannot create PDFs.")
        print("   Windows : install MiKTeX → https://miktex.org/")
        sys.exit(1)

    cfg = load_config()
    batch_size = int(cfg.get("batch_size", 5))
    batch_pdfs = all_pdfs[:batch_size]

    token_history = load_token_stats(STATS_PYTHON_FILE)
    max_tokens = max(compute_max_tokens_python(token_history), _PYTHON_THINKING_BUDGET + 4000)

    print(f"📋  Found {len(all_pdfs)} PDF(s), processing {len(batch_pdfs)} (batch_size={batch_size})")
    print(f"   max_tokens={max_tokens}  thinking_budget={_PYTHON_THINKING_BUDGET}")

    print("\n📤  Building batch requests (Python mode)...")
    requests: list[dict] = []
    pdf_map: dict[str, Path] = {}
    for pdf_path in batch_pdfs:
        req = _build_batch_request(pdf_path, max_tokens)
        requests.append(req)
        pdf_map[req["custom_id"]] = pdf_path
        print(f"   + {pdf_path.name}")

    print(f"\n⏳  Submitting {len(requests)} request(s) to Batch API...")
    results = _submit_and_poll_batch(client, requests)

    usage = Usage(batch=True)
    success_count = 0
    failures: list[tuple[str, str]] = []
    new_samples: list[int] = []

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    for result in results:
        pdf_path = pdf_map.get(result.custom_id)
        name = pdf_path.name if pdf_path else result.custom_id
        print(f"\n{'=' * 60}")
        print(f"📄  {name}")

        if result.result.type == "succeeded":
            message = result.result.message
            usage.add(message.usage)
            new_samples.append(message.usage.output_tokens)
            text_blocks = [b for b in message.content if b.type == "text"]
            if not text_blocks:
                print(f"❌  No text block in response for {name}")
                failures.append((name, "no text block in response"))
                continue
            answer = text_blocks[0].text
            stem = Path(result.custom_id).stem
            output_pdf = OUTPUT_DIR / f"{stem}_python_answer.pdf"
            try:
                create_python_pdf(answer, str(output_pdf), xelatex_path=xelatex_path)
                code = extract_python_code(answer)
                if code:
                    code_py = OUTPUT_DIR / f"{stem}.py"
                    code_py.write_text(code, encoding="utf-8")
                    print(f"🐍  Saved  {code_py.name}")
                if pdf_path:
                    shutil.move(str(pdf_path), DONE_DIR / pdf_path.name)
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

    if new_samples:
        token_history.extend(new_samples)
        save_token_stats(token_history, STATS_PYTHON_FILE)

    elapsed = _fmt_elapsed(time.perf_counter() - t_start)
    print(f"\n{'=' * 60}")
    print(f"Done — {success_count} succeeded, {len(failures)} failed.  [{elapsed}]")

    if failures:
        print(f"\n⚠️  Failed ({len(failures)}):")
        for fname, reason in failures:
            print(f"   • {fname}: {reason}")

    remaining = len(all_pdfs) - batch_size
    if remaining > 0:
        print(f"📂  {remaining} PDF(s) remaining in input/")

    _print_usage_summary(usage)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Add it to .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Single-file mode: python main_python.py <exam.pdf> [output.pdf]
    if len(sys.argv) >= 2:
        pdf_path = Path(sys.argv[1])
        if not pdf_path.exists():
            print(f"Error: file not found – {pdf_path}")
            sys.exit(1)
        output_pdf = (
            Path(sys.argv[2]) if len(sys.argv) >= 3
            else Path(f"{pdf_path.stem}_python_answer.pdf")
        )
        print("🐍  QA Claude – Python Exam (single file)")
        print("=" * 60)
        t_start = time.perf_counter()
        usage = Usage()
        try:
            answer = process_pdf_python(client, str(pdf_path), usage)
            create_python_pdf(answer, str(output_pdf))
            code = extract_python_code(answer)
            if code:
                code_py = output_pdf.parent / f"{pdf_path.stem}.py"
                code_py.write_text(code, encoding="utf-8")
                print(f"🐍  Code saved: {code_py.resolve()}")
        except RuntimeError as exc:
            print(f"\n❌  {exc}")
            sys.exit(1)
        _print_usage_summary(usage)
        print(f"\n✨  Done  →  {output_pdf.resolve()}  [{_fmt_elapsed(time.perf_counter() - t_start)}]")
        return

    # Batch mode
    print("🐍  QA Claude – Python Exam (batch mode)")
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
            print(f"\n⏱   รอ {wait_seconds} วินาที...")
            time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
