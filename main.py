#!/usr/bin/env python3
"""
QA Claude - อ่านโจทย์จากรูปภาพ ถาม Claude และสร้าง PDF ผ่าน XeLaTeX
"""

import re
import base64
import json
import os
import sys
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Windows terminal may use cp874 (Thai) — force UTF-8 so emojis print correctly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ─── Directories & Config ─────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
INPUT_DIR   = BASE_DIR / "input"
DONE_DIR    = BASE_DIR / "input_done"
OUTPUT_DIR  = BASE_DIR / "output"
LOG_DIR     = BASE_DIR / "log"
CONFIG_FILE = BASE_DIR / "config.json"
STATS_FILE  = BASE_DIR / "stats.json"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_DEFAULT_CONFIG: dict = {"batch_size": 5}

# Adaptive max_tokens bounds
_MIN_MAX_TOKENS     = 800
_MAX_MAX_TOKENS     = 4096
_DEFAULT_MAX_TOKENS = 1500   # used when history is too short
_STATS_WINDOW       = 50     # keep last N output-token samples
_STATS_MIN_SAMPLES  = 5      # need at least this many before adapting

# claude-sonnet-4-6 pricing (USD per 1 M tokens)
_PRICE_USD = {
    "input":        3.00,
    "output":      15.00,
    "cache_write":  3.75,   # cache creation (25 % more than base input)
    "cache_read":   0.30,   # cache hit      (90 % cheaper than base input)
}


class Usage:
    """Accumulates token counts and computes estimated cost across multiple API calls."""

    def __init__(self) -> None:
        self.input_tokens        = 0
        self.output_tokens       = 0
        self.cache_write_tokens  = 0
        self.cache_read_tokens   = 0

    def add(self, u) -> None:
        self.input_tokens       += u.input_tokens        or 0
        self.output_tokens      += u.output_tokens       or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens  += getattr(u, "cache_read_input_tokens",     0) or 0

    def cost_usd(self) -> float:
        return (
            self.input_tokens       / 1_000_000 * _PRICE_USD["input"]       +
            self.output_tokens      / 1_000_000 * _PRICE_USD["output"]      +
            self.cache_write_tokens / 1_000_000 * _PRICE_USD["cache_write"] +
            self.cache_read_tokens  / 1_000_000 * _PRICE_USD["cache_read"]
        )

    def summary(self) -> str:
        lines = [
            f"  Input tokens       : {self.input_tokens:,}",
            f"  Output tokens      : {self.output_tokens:,}",
        ]
        if self.cache_write_tokens:
            lines.append(f"  Cache write tokens : {self.cache_write_tokens:,}")
        if self.cache_read_tokens:
            lines.append(f"  Cache read tokens  : {self.cache_read_tokens:,}")
        # lines.append(f"  Est. cost (USD)    : ${self.cost_usd():.4f}")
        cost = self.cost_usd()
        if cost < 0.001:
            lines.append(f"  Est. cost (USD)    : ${cost:.2e} : thb {cost*33:.2f}")   # → $4.39e-05
        else:
            lines.append(f"  Est. cost (USD)    : ${cost:.6f} : thb {cost*33:.2f}")
        return "\n".join(lines)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return {**_DEFAULT_CONFIG, **json.load(f)}
    return _DEFAULT_CONFIG.copy()


# ─── Adaptive max_tokens ──────────────────────────────────────────────────────

def load_token_stats() -> list[int]:
    if STATS_FILE.exists():
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f).get("output_tokens", [])
    return []


def save_token_stats(samples: list[int]) -> None:
    trimmed = samples[-_STATS_WINDOW:]
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump({"output_tokens": trimmed}, f)


def compute_max_tokens(samples: list[int]) -> int:
    """Return adaptive max_tokens from historical output token counts (p90 + 20% buffer)."""
    if len(samples) < _STATS_MIN_SAMPLES:
        return _DEFAULT_MAX_TOKENS
    p90 = sorted(samples)[int(len(samples) * 0.9)]
    adaptive = int(p90 * 1.2)
    return max(_MIN_MAX_TOKENS, min(_MAX_MAX_TOKENS, adaptive))


# ─── Font Resolution ──────────────────────────────────────────────────────────

FONTS_DIR = Path(__file__).parent / "fonts"

_FONT_SEARCH_DIRS = [
    FONTS_DIR,
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/thai-tlwg"),
    Path("/usr/share/fonts/opentype/thai-tlwg"),
]

_FONT_CANDIDATES = [
    ("Laksaman", "Laksaman-Bold",
     ["Laksaman.ttf"],
     ["Laksaman-Bold.ttf", "Laksaman Bold.ttf", "LaksamanBold.ttf"]),
    ("Loma", "Loma Bold",
     ["Loma.ttf"],
     ["Loma-Bold.ttf", "Loma Bold.ttf", "LomaBold.ttf"]),
    ("Tahoma", "Tahoma Bold",
     ["Tahoma.ttf", "tahoma.ttf"],
     ["TahomaB.ttf", "tahomabd.ttf"]),
]


def _find_font(filenames: list[str]) -> Path | None:
    for d in _FONT_SEARCH_DIRS:
        for name in filenames:
            p = d / name
            if p.exists():
                return p
    return None


def resolve_fonts() -> tuple[str, str, str]:
    """Return (fonts_posix_dir, reg_stem, bold_stem) for the best available Thai font."""
    for _, _, reg_files, bold_files in _FONT_CANDIDATES:
        reg_path = _find_font(reg_files)
        if reg_path is None:
            continue
        bold_path = _find_font(bold_files) or reg_path
        print(f"  Font: {reg_path.name}  +  {bold_path.name}")
        return reg_path.parent.as_posix() + "/", reg_path.stem, bold_path.stem
    print("  Warning: No Thai font found, using default.")
    return "", "", ""


# ─── Markdown → LaTeX Conversion ─────────────────────────────────────────────

_INLINE_MATH_RE = re.compile(r"((?<!\$)\$[^$\n]+?\$(?!\$))")


def latex_escape(text: str) -> str:
    """Escape LaTeX special chars in plain text (not inside math mode)."""
    for char, esc in [
        ("\\", "\\textbackslash{}"),
        ("&",  "\\&"),
        ("%",  "\\%"),
        ("#",  "\\#"),
        ("$",  "\\$"),
        ("_",  "\\_"),
        ("{",  "\\{"),
        ("}",  "\\}"),
        ("~",  "\\textasciitilde{}"),
        ("^",  "\\textasciicircum{}"),
    ]:
        text = text.replace(char, esc)
    return text


# Unicode math / Greek chars that Laksaman font cannot render — convert to LaTeX macros.
# Applied AFTER latex_escape so the inserted $ and \ are not re-escaped.
_UNICODE_TO_LATEX: dict[str, str] = {
    # Arithmetic
    "×": "$\\times$",    "÷": "$\\div$",
    "±": "$\\pm$",       "∓": "$\\mp$",
    "·": "$\\cdot$",
    # Comparison
    "≤": "$\\leq$",      "≥": "$\\geq$",
    "≠": "$\\neq$",      "≈": "$\\approx$",
    "∝": "$\\propto$",
    # Superscripts (text-mode so they size-match running text)
    "⁰": "\\textsuperscript{0}",
    "¹": "\\textsuperscript{1}",
    "²": "\\textsuperscript{2}",
    "³": "\\textsuperscript{3}",
    "⁴": "\\textsuperscript{4}",
    "⁵": "\\textsuperscript{5}",
    "⁶": "\\textsuperscript{6}",
    "⁷": "\\textsuperscript{7}",
    "⁸": "\\textsuperscript{8}",
    "⁹": "\\textsuperscript{9}",
    # Misc math
    "√": "$\\surd$",     "∞": "$\\infty$",
    "∂": "$\\partial$",  "°": "$^{\\circ}$",
    # Set / logic
    "∈": "$\\in$",       "∉": "$\\notin$",
    "∩": "$\\cap$",      "∪": "$\\cup$",
    "⊆": "$\\subseteq$", "⊂": "$\\subset$",
    "⊃": "$\\supset$",
    "∀": "$\\forall$",   "∃": "$\\exists$",
    # Calculus
    "∑": "$\\sum$",      "∏": "$\\prod$",
    "∫": "$\\int$",
    # Arrows / logic
    "→": "$\\rightarrow$",   "←": "$\\leftarrow$",
    "↔": "$\\leftrightarrow$",
    "⇒": "$\\Rightarrow$",   "⇐": "$\\Leftarrow$",
    "⇔": "$\\Leftrightarrow$",
    # Greek lowercase
    "α": "$\\alpha$",    "β": "$\\beta$",
    "γ": "$\\gamma$",    "δ": "$\\delta$",
    "ε": "$\\epsilon$",  "θ": "$\\theta$",
    "λ": "$\\lambda$",   "μ": "$\\mu$",
    "π": "$\\pi$",       "σ": "$\\sigma$",
    "φ": "$\\phi$",      "ω": "$\\omega$",
    # Greek uppercase
    "Γ": "$\\Gamma$",    "Δ": "$\\Delta$",
    "Θ": "$\\Theta$",    "Λ": "$\\Lambda$",
    "Π": "$\\Pi$",       "Σ": "$\\Sigma$",
    "Φ": "$\\Phi$",      "Ω": "$\\Omega$",
    # Punctuation
    "…": "\\ldots{}",
    "—": "---",          "–": "--",
    # Geometric / box symbols
    "■": "$\\blacksquare$",  "□": "$\\square$",
    "▲": "$\\blacktriangle$","▼": "$\\blacktriangledown$",
    "●": "$\\bullet$",       "○": "$\\circ$",
    "◆": "$\\blacklozenge$", "◇": "$\\lozenge$",
    "▶": "$\\triangleright$","◀": "$\\triangleleft$",
    # Misc symbols / dingbats (BMP emoji Claude may output)
    "✅": "\\checkmark{}",   "✓": "\\checkmark{}",
    "❌": "$\\times$",        "✗": "$\\times$",
    "⚠": "(!)",
    "★": "$\\star$",          "☆": "$\\star$",
    "♦": "$\\diamond$",
    # Variation selectors (follow emoji — strip them)
    "️": "",            "︎": "",
}


def apply_inline_latex(text: str) -> str:
    """Convert inline markdown and $...$ math to LaTeX markup."""
    parts = _INLINE_MATH_RE.split(text)
    result: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(part)  # $...$ — already valid LaTeX math
        else:
            s = latex_escape(part)
            s = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", s)
            s = re.sub(r"\*(.+?)\*",     r"\\textit{\1}", s)
            s = re.sub(r"`(.+?)`",       r"\\texttt{\1}", s)
            # Convert Unicode math/Greek (after escaping so inserted $ are not re-escaped)
            for uni, ltx in _UNICODE_TO_LATEX.items():
                s = s.replace(uni, ltx)
            # Strip remaining astral-plane emoji (U+10000+) that Laksaman cannot render
            s = re.sub(r"[\U00010000-\U0010FFFF]", "", s)
            result.append(s)
    return "".join(result)


def md_to_latex(text: str) -> str:
    """Convert Claude's markdown (with LaTeX math) to a LaTeX body fragment."""
    lines   = text.splitlines()
    out:    list[str] = []
    i       = 0
    in_item = False
    in_enum = False

    def flush_lists() -> None:
        nonlocal in_item, in_enum
        if in_item:
            out.append("\\end{itemize}")
            in_item = False
        if in_enum:
            out.append("\\end{enumerate}")
            in_enum = False

    while i < len(lines):
        raw      = lines[i].rstrip()
        stripped = raw.lstrip()

        # Blank line
        if not stripped:
            flush_lists()
            out.append("")
            i += 1
            continue

        # Display math  $$...$$  →  \[...\]
        if stripped.startswith("$$"):
            flush_lists()
            inner = stripped[2:]
            if inner.endswith("$$"):
                out.append(f"\\[{inner[:-2].strip()}\\]")
                i += 1
            else:
                parts: list[str] = [inner] if inner.strip() else []
                i += 1
                while i < len(lines):
                    line = lines[i]
                    if "$$" in line:
                        before = line[: line.index("$$")].strip()
                        if before:
                            parts.append(before)
                        i += 1
                        break
                    parts.append(line.rstrip())
                    i += 1
                out.append("\\[" + " ".join(p for p in parts if p) + "\\]")
            continue

        # Fenced code block
        if stripped.startswith("```"):
            flush_lists()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.extend(["\\begin{verbatim}", *code, "\\end{verbatim}"])
            i += 1
            continue

        # Markdown table  |col|col|...
        if stripped.startswith("|"):
            flush_lists()
            table_lines: list[str] = [stripped]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            header: list[str] = []
            body: list[list[str]] = []
            for tline in table_lines:
                cells = [c.strip() for c in tline.strip("|").split("|")]
                if all(re.match(r"^[-: ]+$", c) for c in cells if c):
                    continue  # separator row
                if not header:
                    header = cells
                else:
                    body.append(cells)
            if header:
                ncols = len(header)
                col_spec = "|" + "c|" * ncols
                out.append("\\begin{center}")
                out.append(f"\\begin{{tabular}}{{{col_spec}}}")
                out.append("\\hline")
                hcells = " & ".join(
                    f"\\textbf{{{apply_inline_latex(c)}}}" for c in header
                )
                out.append(f"  {hcells} \\\\")
                out.append("\\hline\\hline")
                for row in body:
                    padded = (row + [""] * ncols)[:ncols]
                    rcells = " & ".join(apply_inline_latex(c) for c in padded)
                    out.append(f"  {rcells} \\\\")
                    out.append("\\hline")
                out.append("\\end{tabular}")
                out.append("\\end{center}")
            continue

        # ATX headings
        if stripped.startswith("### "):
            flush_lists()
            out.append(f"\\subsubsection*{{{apply_inline_latex(stripped[4:])}}}")
        elif stripped.startswith("## "):
            flush_lists()
            out.append(f"\\subsection*{{{apply_inline_latex(stripped[3:])}}}")
        elif stripped.startswith("# "):
            flush_lists()
            out.append(f"\\section*{{{apply_inline_latex(stripped[2:])}}}")

        # Unordered list
        elif stripped.startswith(("- ", "* ", "+ ")):
            if not in_item:
                flush_lists()
                out.append("\\begin{itemize}")
                in_item = True
            out.append(f"  \\item {apply_inline_latex(stripped[2:])}")

        # Ordered list
        elif re.match(r"^\d+[.)]\s", stripped):
            if not in_enum:
                flush_lists()
                out.append("\\begin{enumerate}")
                in_enum = True
            rest = re.split(r"^\d+[.)]\s", stripped, maxsplit=1)
            out.append(f"  \\item {apply_inline_latex(rest[1] if len(rest) > 1 else stripped)}")

        # Horizontal rule
        elif re.match(r"^[-*_]{3,}$", stripped):
            flush_lists()
            out.append("\\vspace{4pt}\\textcolor{slate}{\\hrule}\\vspace{4pt}")

        # Normal paragraph
        else:
            flush_lists()
            out.append(apply_inline_latex(stripped))
            out.append("")

        i += 1

    flush_lists()
    return "\n".join(out)


def parse_sections(text: str) -> dict[str, str]:
    keys    = ["โจทย์", "แนวคิด", "วิธีทำ", "คำตอบ"]
    pattern = (
        r"##\s+(" + "|".join(keys) + r")\s*\n"
        r"(.*?)(?=##\s+(?:" + "|".join(keys) + r")|$)"
    )
    return {
        m.group(1).strip(): m.group(2).strip()
        for m in re.finditer(pattern, text, re.DOTALL)
    }


# ─── LaTeX Document Builder ───────────────────────────────────────────────────

def build_preamble(fonts_path: str, reg_stem: str, bold_stem: str) -> str:
    if reg_stem:
        font_block = (
            f"\\setmainfont[\n"
            f"  Script=Thai,\n"
            f"  Path={fonts_path},\n"
            f"  Extension=.ttf,\n"
            f"  BoldFont={bold_stem},\n"
            f"]{{{reg_stem}}}\n"
            f"\\setmonofont[\n"
            f"  Script=Thai,\n"
            f"  Path={fonts_path},\n"
            f"  Extension=.ttf,\n"
            f"]{{{reg_stem}}}\n"
        )
    else:
        font_block = "\\usepackage{lmodern}\n"

    return (
        "\\documentclass[12pt,a4paper]{article}\n"
        "\\usepackage{fontspec}\n"
        "\\usepackage{polyglossia}\n"
        "\\setmainlanguage{thai}\n"
        "\\setotherlanguage{english}\n"
        + font_block +
        "\\usepackage[a4paper,left=2.5cm,right=2.5cm,top=2cm,bottom=2cm]{geometry}\n"
        "\\usepackage{xcolor}\n"
        "\\usepackage{tcolorbox}\n"
        "\\tcbuselibrary{skins,breakable}\n"
        "\\usepackage{amsmath,amssymb}\n"
        "\\usepackage{enumitem}\n"
        "\n"
        "\\definecolor{primary}{HTML}{2563EB}\n"
        "\\definecolor{secgreen}{HTML}{16A34A}\n"
        "\\definecolor{accent}{HTML}{7C3AED}\n"
        "\\definecolor{answer}{HTML}{EA580C}\n"
        "\\definecolor{muted}{HTML}{64748B}\n"
        "\\definecolor{light}{HTML}{F8FAFC}\n"
        "\\definecolor{slate}{HTML}{CBD5E1}\n"
        "\n"
        "\\newcommand{\\sectbox}[1]{%\n"
        "  \\vspace{10pt}%\n"
        "  {\\large\\bfseries\\color{primary}#1}%\n"
        "  \\par\\nobreak\\vspace{2pt}%\n"
        "  \\textcolor{slate}{\\hrule height 0.8pt}%\n"
        "  \\vspace{6pt}%\n"
        "}\n"
        "\n"
        "\\newtcolorbox{problemenv}{\n"
        "  enhanced, breakable,\n"
        "  colback=light, colframe=primary,\n"
        "  left=8pt, right=8pt, top=6pt, bottom=6pt, arc=4pt\n"
        "}\n"
        "\n"
        "\\newtcolorbox{ansenv}{\n"
        "  enhanced,\n"
        "  colback=orange!8, colframe=answer,\n"
        "  left=8pt, right=8pt, top=6pt, bottom=6pt, arc=4pt,\n"
        "  fontupper=\\bfseries\\color{answer}\n"
        "}\n"
    )


def build_tex(sections: dict[str, str], timestamp: str) -> str:
    johtay  = md_to_latex(sections.get("โจทย์",  ""))
    concept = md_to_latex(sections.get("แนวคิด", ""))
    steps   = md_to_latex(sections.get("วิธีทำ", ""))
    answer  = md_to_latex(sections.get("คำตอบ",  ""))

    return (
        "\\input{preamble.tex}\n"
        "\\begin{document}\n"
        "\n"
        "\\begin{center}\n"
        "  {\\Huge\\bfseries\\color{primary}Poster Answer}\\\\[6pt]\n"
        "  {\\large\\color{muted}\\textenglish{Generated by Poster \\quad " + timestamp + "}}\n"
        "\\end{center}\n"
        "\\vspace{4pt}\n"
        "\\textcolor{primary}{\\rule{\\linewidth}{2pt}}\n"
        "\\vspace{8pt}\n"
        "\n"
        "\\sectbox{1.\\ โจทย์ (Problem)}\n"
        "\\begin{problemenv}\n"
        + johtay + "\n"
        "\\end{problemenv}\n"
        "\n"
        "\\vspace{6pt}\n"
        "\\sectbox{2.\\ แนวคิด (Concept)}\n"
        + concept + "\n"
        "\n"
        "\\sectbox{3.\\ วิธีทำ (Solution)}\n"
        + steps + "\n"
        "\n"
        "\\sectbox{4.\\ คำตอบ (Answer)}\n"
        "\\begin{ansenv}\n"
        + answer + "\n"
        "\\end{ansenv}\n"
        "\n"
        "\\end{document}\n"
    )


# ─── Cached System Prompts ────────────────────────────────────────────────────

# _ANSWER_SYSTEM = (
#     "ตอบโจทย์ที่ user ส่งมาเป็นภาษาไทย โดยจัดรูปแบบตามหัวข้อด้านล่างนี้เท่านั้น "
#     "(ห้ามเพิ่มหรือเปลี่ยนชื่อหัวข้อ):\n\n"
#     "## โจทย์\n"
#     "[สรุปโจทย์ที่ได้รับ]\n\n"
#     "## แนวคิด\n"
#     "[อธิบายทฤษฎีหรือวิธีการที่ใช้ เป็นภาษาไทยปนศัพท์เทคนิคภาษาอังกฤษ "
#     "เหมาะสำหรับนักเรียนระดับมัธยมปลาย ให้เข้าใจว่าใช้หลักการใดและทำไม]\n\n"
#     "## วิธีทำ\n"
#     "[แสดงขั้นตอนการแก้โจทย์อย่างละเอียดเป็นลำดับขั้น]\n\n"
#     "## คำตอบ\n"
#     "[คำตอบสุดท้ายพร้อมหน่วย (ถ้ามี)]\n\n"
#     "---\n"
#     "กฎการเขียนสัญลักษณ์คณิตศาสตร์ (ต้องปฏิบัติตามเสมอ):\n"
#     "- Inline math: ใช้ $...$ เช่น $x^2 + y^2 = r^2$ ภายในย่อหน้า\n"
#     "- Display math: ใช้ $$...$$ บนบรรทัดเดี่ยวของตัวเอง เช่น\n"
#     "  $$\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$$\n"
#     "- ใช้ LaTeX syntax: \\frac{}{}, \\sqrt{}, \\sum_{i=1}^{n}, \\int, ^, _"
# )

# _ANSWER_SYSTEM = (
#     "You are a math and computer science tutor for Thai high school students "
#     "(middle to upper secondary level, e.g. POSN olympiad prep).\n\n"

#     "The user will send you an image of an exam or homework question. "
#     "Read the question from the image, then provide a complete answer.\n\n"

#     "Always respond in Thai, except for technical terms, variable names, and LaTeX.\n\n"

#     "Structure your response using EXACTLY these markdown headings — no additions or renames:\n\n"
#     "## โจทย์\n"
#     "Restate the problem clearly and concisely in Thai.\n\n"
#     "## แนวคิด\n"
#     "Explain the underlying theory or technique in Thai mixed with English technical terms. "
#     "Focus on *why* this approach works, at a level suitable for a motivated high-school student.\n\n"
#     "## วิธีทำ\n"
#     "Show the full step-by-step solution. Number each step. "
#     "Justify non-obvious transitions.\n\n"
#     "## คำตอบ\n"
#     "State the final answer, including units if applicable.\n\n"

#     "---\n"
#     "Math formatting rules (always follow):\n"
#     "- Inline math: $...$ — e.g. $x^2 + y^2 = r^2$\n"
#     "- Display math: $$...$$ on its own line — e.g.\n"
#     "  $$\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$$\n"
#     "- Use LaTeX: \\frac{}{}, \\sqrt{}, \\sum_{i=1}^{n}, \\binom{n}{k}, \\int, ^, _\n"
#     "- Never use Unicode math symbols (×, ÷, ²) — use LaTeX equivalents instead.\n"
#     "- Never use emoji or decorative symbols (✅, ❌, 🎯, 💡, etc.) anywhere in the response."
# )

_ANSWER_SYSTEM = (
    "You are a math and computer science tutor for Thai high school students "
    "(middle to upper secondary level, e.g. POSN olympiad prep).\n\n"

    "The user will send you an image of an exam or homework question. "
    "Read the question from the image, then provide a complete answer.\n\n"

    "Always respond in Thai, except for technical terms, variable names, and LaTeX.\n\n"

    "Structure your response using EXACTLY these markdown headings — no additions or renames:\n\n"
    "## โจทย์\n"
    "Restate the problem clearly and concisely in Thai. "
    "Do NOT copy the problem verbatim — summarise in 1-2 sentences.\n\n"
    "## แนวคิด\n"
    "Explain the underlying theory or technique in Thai mixed with English technical terms. "
    "Focus on *why* this approach works, at a level suitable for a motivated high-school student. "
    "Keep this section under 100 words.\n\n"
    "## วิธีทำ\n"
    "Show the full step-by-step solution. Number each step. "
    "Justify non-obvious transitions. "
    "Skip trivial arithmetic — show key steps only.\n\n"
    "## คำตอบ\n"
    "State the final answer, including units if applicable. "
    "One sentence maximum.\n\n"

    "Aim for a total response length of 400-700 tokens. "
    "Be concise and precise — this is an exam solution, not a textbook.\n\n"

    "---\n"
    "Math formatting rules (always follow):\n"
    "- Inline math: $...$ — e.g. $x^2 + y^2 = r^2$\n"
    "- Display math: $$...$$ on its own line — e.g.\n"
    "  $$\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$$\n"
    "- Use LaTeX: \\frac{}{}, \\sqrt{}, \\sum_{i=1}^{n}, \\binom{n}{k}, \\int, ^, _\n"
    "- Never use Unicode math symbols (×, ÷, ²) — use LaTeX equivalents instead.\n"
    "- Never use emoji or decorative symbols (✅, ❌, 🎯, 💡, etc.) anywhere in the response."
)


def _fmt_usage(u) -> str:
    """Format token usage including cache stats."""
    parts = [f"in={u.input_tokens}", f"out={u.output_tokens}"]
    if hasattr(u, "cache_creation_input_tokens") and u.cache_creation_input_tokens:
        parts.append(f"cache_write={u.cache_creation_input_tokens}")
    if hasattr(u, "cache_read_input_tokens") and u.cache_read_input_tokens:
        parts.append(f"cache_read={u.cache_read_input_tokens}")
    return "  ".join(parts)


# ─── Image & Claude Helpers ───────────────────────────────────────────────────

def image_to_base64(path: str) -> tuple[str, str]:
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".gif": "image/gif",  ".webp": "image/webp",
    }
    media_type = ext_map.get(Path(path).suffix.lower(), "image/jpeg")
    with open(path, "rb") as fh:
        return base64.standard_b64encode(fh.read()).decode(), media_type


def process_image(client: anthropic.Anthropic, image_path: str, usage: Usage) -> str:
    """Send image to Claude in one call: extract question + return structured answer."""
    stats = load_token_stats()
    max_tok = compute_max_tokens(stats)

    print(f"\n📷  Processing: {image_path}\n" + "─" * 60)
    print(f"   max_tokens={max_tok}  (history={len(stats)} samples)")
    img_data, media_type = image_to_base64(image_path)
    parts: list[str] = []

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tok,
        system=[{
            "type": "text",
            "text": _ANSWER_SYSTEM,
        }],
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                {"type": "text",  "text": "กรุณาตอบโจทย์จากรูปภาพนี้"},
            ],
        }],
    ) as stream:
        for text in stream.text_stream:
            parts.append(text)
            print(text, end="", flush=True)

    msg = stream.get_final_message()
    usage.add(msg.usage)

    stats.append(msg.usage.output_tokens)
    save_token_stats(stats)

    print(f"\n   [DEBUG] raw usage: {msg.usage}")
    print(f"   tokens: {_fmt_usage(msg.usage)}")
    print("─" * 60)
    return "".join(parts)


# ─── PDF Creation via XeLaTeX ─────────────────────────────────────────────────

def _find_xelatex() -> str | None:
    """Return the path to xelatex, searching MiKTeX locations if not in PATH."""
    if found := shutil.which("xelatex"):
        return found
    candidates = [
        Path.home() / "AppData/Local/Programs/MiKTeX/miktex/bin/x64/xelatex.exe",
        Path("C:/Program Files/MiKTeX/miktex/bin/x64/xelatex.exe"),
        Path("C:/Program Files (x86)/MiKTeX/miktex/bin/xelatex.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def create_pdf(answer: str, output_path: str) -> None:
    xelatex = _find_xelatex()
    if not xelatex:
        print("❌  xelatex not found.")
        print("   Windows : install MiKTeX → https://miktex.org/")
        print("   Linux   : sudo apt install texlive-xetex texlive-lang-other")
        sys.exit(1)

    print("\n📄  Resolving fonts...")
    fonts_path, reg_stem, bold_stem = resolve_fonts()

    tmp = Path(tempfile.mkdtemp(prefix="qalatex_"))
    print(f"📄  Build dir: {tmp}")

    (tmp / "preamble.tex").write_text(
        build_preamble(fonts_path, reg_stem, bold_stem), encoding="utf-8"
    )

    timestamp = datetime.now().strftime("%d %B %Y, %H:%M")
    sections  = parse_sections(answer)
    (tmp / "output.tex").write_text(
        build_tex(sections, timestamp), encoding="utf-8"
    )

    print("📄  Compiling (XeLaTeX)...")
    log_lines: list[str] = []
    ok = True
    for _ in range(2):
        proc = subprocess.run(
            [xelatex, "-interaction=nonstopmode", "output.tex"],
            cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        log_lines = proc.stdout.splitlines()
        if proc.returncode != 0:
            ok = False

    # Save log to log/ folder (always, success or failure)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem    = Path(output_path).stem
    log_dst = LOG_DIR / f"{stem}.log"
    src_log = tmp / "output.log"
    if src_log.exists():
        shutil.copy2(src_log, log_dst)
    else:
        log_dst.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"📋  Log saved: {log_dst.resolve()}")

    if not ok:
        print("❌  Compilation failed. Errors:")
        for line in log_lines:
            if line.startswith(("!", "Error", "LaTeX Warning: Font")):
                print(f"   {line}")
        sys.exit(1)

    pdf_src = tmp / "output.pdf"
    if not pdf_src.exists():
        print("❌  output.pdf not produced.")
        sys.exit(1)

    shutil.copy2(pdf_src, output_path)
    print(f"✅  PDF saved: {Path(output_path).resolve()}")


# ─── Batch Processing ─────────────────────────────────────────────────────────

def process_one(client: anthropic.Anthropic, img_path: Path, usage: Usage) -> None:
    """Process a single image: extract → answer → PDF, then move to done."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    output_pdf = OUTPUT_DIR / f"{img_path.stem}_answer.pdf"

    answer = process_image(client, str(img_path), usage)
    create_pdf(answer, str(output_pdf))

    shutil.move(str(img_path), DONE_DIR / img_path.name)
    print(f"📦  Moved  {img_path.name}  →  input_done/")


def run_batch(client: anthropic.Anthropic) -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    images = sorted(
        [f for f in INPUT_DIR.iterdir() if f.suffix.lower() in _IMAGE_EXTS]
    )

    if not images:
        print("📂  input/ is empty — nothing to process.")
        return

    batch_size = int(load_config().get("batch_size", 5))
    batch      = images[:batch_size]

    print(f"📋  Found {len(images)} image(s), processing {len(batch)} (batch_size={batch_size})")

    usage = Usage()
    ok = err = 0
    for img_path in batch:
        print(f"\n{'=' * 60}")
        print(f"🖼   {img_path.name}")
        try:
            process_one(client, img_path, usage)
            ok += 1
        except Exception as exc:
            print(f"❌  Failed: {exc}")
            err += 1

    print(f"\n{'=' * 60}")
    print(f"Done — {ok} succeeded, {err} failed.")
    if images[batch_size:]:
        print(f"📂  {len(images) - batch_size} image(s) remaining in input/")
    print(f"\n── Token Usage ──────────────────────────────────────────")
    print(usage.summary())


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Add it to .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Single-file mode: python main.py <image> [output.pdf]
    if len(sys.argv) >= 2:
        img = Path(sys.argv[1])
        if not img.exists():
            print(f"Error: file not found – {img}")
            sys.exit(1)
        out = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(f"{img.stem}_answer.pdf")
        print("🚀  QA Claude – single file mode")
        print("=" * 60)
        usage  = Usage()
        answer = process_image(client, str(img), usage)
        create_pdf(answer, str(out))
        print(f"\n── Token Usage ──────────────────────────────────────────")
        print(usage.summary())
        print(f"\n✨  Done  →  {out.resolve()}")
        return

    # Batch mode: read from input/, output to output/, move done to input_done/
    print("🚀  QA Claude – batch mode")
    print("=" * 60)
    run_batch(client)


if __name__ == "__main__":
    main()
