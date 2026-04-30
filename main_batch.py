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
import time
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Windows terminal may use cp874 (Thai) — force UTF-8 so emojis print correctly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ─── Directories & Config ─────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
INPUT_DIR = BASE_DIR / "input"
DONE_DIR = BASE_DIR / "input_done"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "log"
CONFIG_FILE = BASE_DIR / "config.json"
STATS_FILE = BASE_DIR / "stats.json"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_DEFAULT_CONFIG: dict = {
    "batch_size": 5,
    "number_of_repetitions": 1,
    "wait_time_between_repetitions": 60,
}

# Adaptive max_tokens bounds
_MIN_MAX_TOKENS = 800
_MAX_MAX_TOKENS = 4096
_DEFAULT_MAX_TOKENS = 1500  # used when history is too short
_STATS_WINDOW = 50          # keep last N output-token samples
_STATS_MIN_SAMPLES = 5      # need at least this many before adapting

# claude-sonnet-4-6 pricing (USD per 1 M tokens)
_PRICE_USD = {
    "input": 3.00,
    "output": 15.00,
    "cache_write": 3.75,  # cache creation (25% more than base input)
    "cache_read": 0.30,   # cache hit (90% cheaper than base input)
}

# Batch API pricing — 50% discount from standard
_PRICE_USD_BATCH = {
    "input": 1.50,
    "output": 7.50,
    "cache_write": 1.875,
    "cache_read": 0.15,
}


class Usage:
    """Accumulates token counts and computes estimated cost across multiple API calls."""

    def __init__(self, batch: bool = False) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_tokens = 0
        self.cache_read_tokens = 0
        self._price = _PRICE_USD_BATCH if batch else _PRICE_USD

    def add(self, api_usage) -> None:
        self.input_tokens += api_usage.input_tokens or 0
        self.output_tokens += api_usage.output_tokens or 0
        self.cache_write_tokens += getattr(api_usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(api_usage, "cache_read_input_tokens", 0) or 0

    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * self._price["input"]
            + self.output_tokens / 1_000_000 * self._price["output"]
            + self.cache_write_tokens / 1_000_000 * self._price["cache_write"]
            + self.cache_read_tokens / 1_000_000 * self._price["cache_read"]
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
        cost = self.cost_usd()
        if cost < 0.001:
            lines.append(f"  Est. cost (USD)    : ${cost:.2e} : thb {cost * 33:.2f}")
        else:
            lines.append(f"  Est. cost (USD)    : ${cost:.6f} : thb {cost * 33:.2f}")
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
    ninetieth_percentile = sorted(samples)[int(len(samples) * 0.9)]
    adaptive_limit = int(ninetieth_percentile * 1.2)
    return max(_MIN_MAX_TOKENS, min(_MAX_MAX_TOKENS, adaptive_limit))


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
    for search_dir in _FONT_SEARCH_DIRS:
        for filename in filenames:
            candidate_path = search_dir / filename
            if candidate_path.exists():
                return candidate_path
    return None


def resolve_fonts() -> tuple[str, str, str]:
    """Return (fonts_posix_dir, reg_stem, bold_stem) for the best available Thai font."""
    for _, _, regular_files, bold_files in _FONT_CANDIDATES:
        regular_font_path = _find_font(regular_files)
        if regular_font_path is None:
            continue
        bold_font_path = _find_font(bold_files) or regular_font_path
        print(f"  Font: {regular_font_path.name}  +  {bold_font_path.name}")
        return regular_font_path.parent.as_posix() + "/", regular_font_path.stem, bold_font_path.stem
    print("  Warning: No Thai font found, using default.")
    return "", "", ""


# ─── Markdown → LaTeX Conversion ─────────────────────────────────────────────

_INLINE_MATH_RE = re.compile(r"((?<!\$)\$[^$\n]+?\$(?!\$))")


def latex_escape(text: str) -> str:
    """Escape LaTeX special chars in plain text (not inside math mode)."""
    for char, escaped in [
        ("\\", "\\textbackslash{}"),
        ("&", "\\&"),
        ("%", "\\%"),
        ("#", "\\#"),
        ("$", "\\$"),
        ("_", "\\_"),
        ("{", "\\{"),
        ("}", "\\}"),
        ("~", "\\textasciitilde{}"),
        ("^", "\\textasciicircum{}"),
    ]:
        text = text.replace(char, escaped)
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
    text_parts = _INLINE_MATH_RE.split(text)
    output_parts: list[str] = []
    for index, part in enumerate(text_parts):
        if index % 2 == 1:
            output_parts.append(part)  # $...$ — already valid LaTeX math
        else:
            escaped_text = latex_escape(part)
            escaped_text = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", escaped_text)
            escaped_text = re.sub(r"\*(.+?)\*", r"\\textit{\1}", escaped_text)
            escaped_text = re.sub(r"`(.+?)`", r"\\texttt{\1}", escaped_text)
            # Convert Unicode math/Greek (after escaping so inserted $ are not re-escaped)
            for unicode_char, latex_macro in _UNICODE_TO_LATEX.items():
                escaped_text = escaped_text.replace(unicode_char, latex_macro)
            # Strip remaining astral-plane emoji (U+10000+) that Laksaman cannot render
            escaped_text = re.sub(r"[\U00010000-\U0010FFFF]", "", escaped_text)
            output_parts.append(escaped_text)
    return "".join(output_parts)


def _render_display_math(lines: list[str], i: int) -> tuple[str, int]:
    """Parse $$...$$ block starting at line i; return (latex_line, next_i)."""
    inner = lines[i].lstrip()[2:]
    if inner.endswith("$$"):
        return f"\\[{inner[:-2].strip()}\\]", i + 1
    math_parts = [inner] if inner.strip() else []
    i += 1
    while i < len(lines):
        line = lines[i]
        if "$$" in line:
            before = line[: line.index("$$")].strip()
            if before:
                math_parts.append(before)
            return "\\[" + " ".join(p for p in math_parts if p) + "\\]", i + 1
        math_parts.append(line.rstrip())
        i += 1
    return "\\[" + " ".join(p for p in math_parts if p) + "\\]", i


def _render_code_block(lines: list[str], i: int) -> tuple[list[str], int]:
    """Parse ```...``` block starting at line i; return (latex_lines, next_i)."""
    code_lines: list[str] = []
    i += 1
    while i < len(lines) and not lines[i].strip().startswith("```"):
        code_lines.append(lines[i])
        i += 1
    return ["\\begin{verbatim}", *code_lines, "\\end{verbatim}"], i + 1


def _render_table(lines: list[str], i: int) -> tuple[list[str], int]:
    """Parse markdown table starting at line i; return (latex_lines, next_i)."""
    table_lines: list[str] = [lines[i].strip()]
    i += 1
    while i < len(lines) and lines[i].strip().startswith("|"):
        table_lines.append(lines[i].strip())
        i += 1

    header: list[str] = []
    body: list[list[str]] = []
    for table_line in table_lines:
        cells = [c.strip() for c in table_line.strip("|").split("|")]
        if all(re.match(r"^[-: ]+$", c) for c in cells if c):
            continue  # separator row
        if not header:
            header = cells
        else:
            body.append(cells)

    if not header:
        return [], i

    ncols = len(header)
    col_spec = "|" + "c|" * ncols
    latex_lines: list[str] = [
        "\\begin{center}",
        f"\\begin{{tabular}}{{{col_spec}}}",
        "\\hline",
    ]
    header_cells = " & ".join(f"\\textbf{{{apply_inline_latex(c)}}}" for c in header)
    latex_lines.append(f"  {header_cells} \\\\")
    latex_lines.append("\\hline\\hline")
    for row in body:
        padded = (row + [""] * ncols)[:ncols]
        row_cells = " & ".join(apply_inline_latex(c) for c in padded)
        latex_lines.append(f"  {row_cells} \\\\")
        latex_lines.append("\\hline")
    latex_lines.extend(["\\end{tabular}", "\\end{center}"])
    return latex_lines, i


def md_to_latex(text: str) -> str:
    """Convert Claude's markdown (with LaTeX math) to a LaTeX body fragment."""
    lines = text.splitlines()
    output: list[str] = []
    i = 0
    in_bullet_list = False
    in_ordered_list = False

    def flush_lists() -> None:
        nonlocal in_bullet_list, in_ordered_list
        if in_bullet_list:
            output.append("\\end{itemize}")
            in_bullet_list = False
        if in_ordered_list:
            output.append("\\end{enumerate}")
            in_ordered_list = False

    while i < len(lines):
        line_content = lines[i].rstrip().lstrip()

        if not line_content:
            flush_lists()
            output.append("")
            i += 1
            continue

        if line_content.startswith("$$"):
            flush_lists()
            latex_line, i = _render_display_math(lines, i)
            output.append(latex_line)
            continue

        if line_content.startswith("```"):
            flush_lists()
            latex_lines, i = _render_code_block(lines, i)
            output.extend(latex_lines)
            continue

        if line_content.startswith("|"):
            flush_lists()
            latex_lines, i = _render_table(lines, i)
            output.extend(latex_lines)
            continue

        if line_content.startswith("### "):
            flush_lists()
            output.append(f"\\subsubsection*{{{apply_inline_latex(line_content[4:])}}}")
        elif line_content.startswith("## "):
            flush_lists()
            output.append(f"\\subsection*{{{apply_inline_latex(line_content[3:])}}}")
        elif line_content.startswith("# "):
            flush_lists()
            output.append(f"\\section*{{{apply_inline_latex(line_content[2:])}}}")
        elif line_content.startswith(("- ", "* ", "+ ")):
            if not in_bullet_list:
                flush_lists()
                output.append("\\begin{itemize}")
                in_bullet_list = True
            output.append(f"  \\item {apply_inline_latex(line_content[2:])}")
        elif re.match(r"^\d+[.)]\s", line_content):
            if not in_ordered_list:
                flush_lists()
                output.append("\\begin{enumerate}")
                in_ordered_list = True
            rest = re.split(r"^\d+[.)]\s", line_content, maxsplit=1)
            output.append(f"  \\item {apply_inline_latex(rest[1] if len(rest) > 1 else line_content)}")
        elif re.match(r"^[-*_]{3,}$", line_content):
            flush_lists()
            output.append("\\vspace{4pt}\\textcolor{slate}{\\hrule}\\vspace{4pt}")
        else:
            flush_lists()
            output.append(apply_inline_latex(line_content))
            output.append("")

        i += 1

    flush_lists()
    return "\n".join(output)


def parse_sections(text: str) -> dict[str, str]:
    section_headings = ["โจทย์", "แนวคิด", "วิธีทำ", "คำตอบ"]
    section_pattern = (
        r"##\s+(" + "|".join(section_headings) + r")\s*\n"
        r"(.*?)(?=##\s+(?:" + "|".join(section_headings) + r")|$)"
    )
    return {
        m.group(1).strip(): m.group(2).strip()
        for m in re.finditer(section_pattern, text, re.DOTALL)
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
        + font_block
        + "\\usepackage[a4paper,left=2.5cm,right=2.5cm,top=2cm,bottom=2cm]{geometry}\n"
        "\\usepackage{xcolor}\n"
        "\\usepackage{tcolorbox}\n"
        "\\tcbuselibrary{skins,breakable}\n"
        "\\usepackage{amsmath,amssymb}\n"
        "\\usepackage{cancel}\n"
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
    problem_body = md_to_latex(sections.get("โจทย์", ""))
    concept_body = md_to_latex(sections.get("แนวคิด", ""))
    solution_body = md_to_latex(sections.get("วิธีทำ", ""))
    answer_body = md_to_latex(sections.get("คำตอบ", ""))

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
        + problem_body + "\n"
        "\\end{problemenv}\n"
        "\n"
        "\\vspace{6pt}\n"
        "\\sectbox{2.\\ แนวคิด (Concept)}\n"
        + concept_body + "\n"
        "\n"
        "\\sectbox{3.\\ วิธีทำ (Solution)}\n"
        + solution_body + "\n"
        "\n"
        "\\sectbox{4.\\ คำตอบ (Answer)}\n"
        "\\begin{ansenv}\n"
        + answer_body + "\n"
        "\\end{ansenv}\n"
        "\n"
        "\\end{document}\n"
    )


# ─── System Prompt ────────────────────────────────────────────────────────────

_ANSWER_SYSTEM = (
    "You are a math and computer science tutor for Thai high school students "
    "(middle to upper secondary level, e.g. POSN olympiad prep).\n\n"

    "Solve with absolute precision, as the provided multiple-choice options may not contain the correct answer.\n\n"

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
    "Keep this section under 200 words.\n\n"
    "## วิธีทำ\n"
    "Show the full step-by-step solution. Number each step. "
    "Justify non-obvious transitions. "
    "Skip trivial arithmetic — show key steps only.\n\n"
    "## คำตอบ\n"
    "State the final answer, including units if applicable. "
    "One sentence maximum.\n\n"

    "Aim for a total response length of 400-1000 tokens. "
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


def _format_api_usage(api_usage) -> str:
    """Format token usage including cache stats as a compact string."""
    parts = [f"in={api_usage.input_tokens}", f"out={api_usage.output_tokens}"]
    if getattr(api_usage, "cache_creation_input_tokens", 0):
        parts.append(f"cache_write={api_usage.cache_creation_input_tokens}")
    if getattr(api_usage, "cache_read_input_tokens", 0):
        parts.append(f"cache_read={api_usage.cache_read_input_tokens}")
    return "  ".join(parts)


# ─── Batch API Helpers ────────────────────────────────────────────────────────

def _sanitize_custom_id(name: str) -> str:
    """Strip characters not allowed by the Batch API custom_id pattern."""
    import re
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


# ─── Image & Claude Helpers ───────────────────────────────────────────────────

def image_to_base64(path: str) -> tuple[str, str]:
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }
    media_type = ext_map.get(Path(path).suffix.lower(), "image/jpeg")
    with open(path, "rb") as file_handle:
        return base64.standard_b64encode(file_handle.read()).decode(), media_type


def process_image(client: anthropic.Anthropic, image_path: str, usage: Usage) -> str:
    """Send image to Claude in one call: extract question + return structured answer."""
    token_history = load_token_stats()
    max_output_tokens = compute_max_tokens(token_history)

    print(f"\n📷  Processing: {image_path}\n" + "─" * 60)
    print(f"   max_tokens={max_output_tokens}  (history={len(token_history)} samples)")
    image_b64_data, media_type = image_to_base64(image_path)
    response_chunks: list[str] = []

    t0 = time.perf_counter()
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_output_tokens,
        system=[{"type": "text", "text": _ANSWER_SYSTEM}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64_data}},
                {"type": "text", "text": "กรุณาตอบโจทย์จากรูปภาพนี้"},
            ],
        }],
    ) as stream:
        for text_chunk in stream.text_stream:
            response_chunks.append(text_chunk)
            print(text_chunk, end="", flush=True)

    final_message = stream.get_final_message()
    usage.add(final_message.usage)

    token_history.append(final_message.usage.output_tokens)
    save_token_stats(token_history)

    print(f"\n   [DEBUG] raw usage: {final_message.usage}")
    print(f"   tokens: {_format_api_usage(final_message.usage)}")
    print(f"   elapsed: {_fmt_elapsed(time.perf_counter() - t0)}")
    print("─" * 60)
    return "".join(response_chunks)


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
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _write_latex_files(
    build_dir: Path, answer: str, fonts_path: str, reg_stem: str, bold_stem: str
) -> None:
    """Write preamble.tex and output.tex into the build directory."""
    timestamp = datetime.now().strftime("%d %B %Y, %H:%M")
    sections = parse_sections(answer)
    (build_dir / "preamble.tex").write_text(
        build_preamble(fonts_path, reg_stem, bold_stem), encoding="utf-8"
    )
    (build_dir / "output.tex").write_text(build_tex(sections, timestamp), encoding="utf-8")


def _compile_xelatex(xelatex_path: str, build_dir: Path) -> tuple[bool, list[str]]:
    """Run xelatex twice (for cross-references); return (success, log_lines)."""
    log_lines: list[str] = []
    compilation_succeeded = True
    for _ in range(2):
        proc = subprocess.run(
            [xelatex_path, "-interaction=nonstopmode", "output.tex"],
            cwd=build_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        log_lines = proc.stdout.splitlines()
        if proc.returncode != 0:
            compilation_succeeded = False
    return compilation_succeeded, log_lines


def _save_build_log(build_dir: Path, log_lines: list[str], output_path: str) -> Path:
    """Copy the XeLaTeX log to LOG_DIR; return the destination path."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    output_stem = Path(output_path).stem
    log_destination = LOG_DIR / f"{output_stem}.log"
    build_log_path = build_dir / "output.log"
    if build_log_path.exists():
        shutil.copy2(build_log_path, log_destination)
    else:
        log_destination.write_text("\n".join(log_lines), encoding="utf-8")
    return log_destination


def create_pdf(answer: str, output_path: str, xelatex_path: str | None = None) -> None:
    if xelatex_path is None:
        xelatex_path = _find_xelatex()
    if not xelatex_path:
        raise RuntimeError("xelatex not found — install MiKTeX (Windows) or texlive-xetex (Linux)")

    print("\n📄  Resolving fonts...")
    fonts_path, reg_stem, bold_stem = resolve_fonts()

    build_dir = Path(tempfile.mkdtemp(prefix="qalatex_"))
    print(f"📄  Build dir: {build_dir}")

    _write_latex_files(build_dir, answer, fonts_path, reg_stem, bold_stem)

    print("📄  Compiling (XeLaTeX)...")
    compilation_succeeded, log_lines = _compile_xelatex(xelatex_path, build_dir)

    log_destination = _save_build_log(build_dir, log_lines, output_path)
    print(f"📋  Log saved: {log_destination.resolve()}")

    if not compilation_succeeded:
        error_lines = [l for l in log_lines if l.startswith(("!", "Error", "LaTeX Warning: Font"))]
        print("❌  Compilation failed. Errors:")
        for line in error_lines:
            print(f"   {line}")
        raise RuntimeError("XeLaTeX compilation failed — see log: " + str(log_destination))

    compiled_pdf = build_dir / "output.pdf"
    if not compiled_pdf.exists():
        raise RuntimeError("XeLaTeX compiled but produced no PDF")

    shutil.copy2(compiled_pdf, output_path)
    print(f"✅  PDF saved: {Path(output_path).resolve()}")


# ─── Batch Processing ─────────────────────────────────────────────────────────

def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {seconds % 60:.1f}s"


def _print_usage_summary(usage: Usage) -> None:
    print(f"\n── Token Usage ──────────────────────────────────────────")
    print(usage.summary())


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
    failures: list[tuple[str, str]] = []  # (filename, reason)
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
    print("🚀  QA Claude – batch mode")
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
