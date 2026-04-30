#!/usr/bin/env python3
"""
Shared utilities for QA Claude: config, token stats, LaTeX generation,
image encoding, Claude API helpers, and PDF creation.
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
STATS_PYTHON_FILE = BASE_DIR / "stats_python.json"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_PDF_EXTS = {".pdf"}
_DEFAULT_CONFIG: dict = {
    "batch_size": 5,
    "number_of_repetitions": 1,
    "wait_time_between_repetitions": 60,
}

# Adaptive max_tokens bounds
_MIN_MAX_TOKENS = 800
_MAX_MAX_TOKENS = 4096
_MAX_MAX_TOKENS_PYTHON = 8192  # claude-sonnet-4-6 model ceiling
_DEFAULT_MAX_TOKENS = 1500
_DEFAULT_MAX_TOKENS_PYTHON = _MAX_MAX_TOKENS_PYTHON
_STATS_WINDOW = 50
_STATS_MIN_SAMPLES = 5

# Extended thinking for Python mode (thinking tokens count toward max_tokens)
_PYTHON_THINKING_BUDGET = 4000

# claude-sonnet-4-6 pricing (USD per 1 M tokens)
_PRICE_USD = {
    "input": 3.00,
    "output": 15.00,
    "cache_write": 3.75,
    "cache_read": 0.30,
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

def load_token_stats(stats_file: Path = STATS_FILE) -> list[int]:
    if stats_file.exists():
        with open(stats_file, encoding="utf-8") as f:
            return json.load(f).get("output_tokens", [])
    return []


def save_token_stats(samples: list[int], stats_file: Path = STATS_FILE) -> None:
    trimmed = samples[-_STATS_WINDOW:]
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump({"output_tokens": trimmed}, f)


def compute_max_tokens(samples: list[int]) -> int:
    """Return adaptive max_tokens from historical output token counts (p90 + 20% buffer)."""
    if len(samples) < _STATS_MIN_SAMPLES:
        return _DEFAULT_MAX_TOKENS
    ninetieth_percentile = sorted(samples)[int(len(samples) * 0.9)]
    adaptive_limit = int(ninetieth_percentile * 1.2)
    return max(_MIN_MAX_TOKENS, min(_MAX_MAX_TOKENS, adaptive_limit))

def compute_max_tokens_python(samples: list[int]) -> int:
    """Return adaptive max_tokens from historical output token counts (p90 + 20% buffer)."""
    if len(samples) < _STATS_MIN_SAMPLES:
        return _DEFAULT_MAX_TOKENS_PYTHON
    ninetieth_percentile = sorted(samples)[int(len(samples) * 0.9)]
    adaptive_limit = int(ninetieth_percentile * 1.2)
    return max(_MIN_MAX_TOKENS, min(_MAX_MAX_TOKENS_PYTHON, adaptive_limit))


# ─── Font Resolution ──────────────────────────────────────────────────────────

FONTS_DIR = BASE_DIR / "fonts"

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
    lang = lines[i].strip()[3:].strip().lower()
    code_lines: list[str] = []
    i += 1
    while i < len(lines) and not lines[i].strip().startswith("```"):
        code_lines.append(lines[i])
        i += 1
    if lang == "python":
        return ["\\begin{lstlisting}[style=pythonstyle]", *code_lines, "\\end{lstlisting}"], i + 1
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
        "\\usepackage{listings}\n"
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
        "\n"
        "\\definecolor{codebg}{HTML}{F0F4FF}\n"
        "\\definecolor{codeframe}{HTML}{4A6FA5}\n"
        "\\definecolor{codekw}{HTML}{0000CD}\n"
        "\\definecolor{codestr}{HTML}{B22222}\n"
        "\\definecolor{codecmt}{HTML}{228B22}\n"
        "\n"
        "\\lstdefinestyle{pythonstyle}{\n"
        "  language=Python,\n"
        "  basicstyle=\\ttfamily\\footnotesize,\n"
        "  keywordstyle=\\color{codekw}\\bfseries,\n"
        "  stringstyle=\\color{codestr},\n"
        "  commentstyle=\\color{codecmt}\\itshape,\n"
        "  numberstyle=\\tiny\\color{gray},\n"
        "  numbers=left,\n"
        "  stepnumber=1,\n"
        "  numbersep=10pt,\n"
        "  backgroundcolor=\\color{codebg},\n"
        "  showspaces=false,\n"
        "  showstringspaces=false,\n"
        "  breaklines=true,\n"
        "  breakatwhitespace=true,\n"
        "  frame=single,\n"
        "  rulecolor=\\color{codeframe},\n"
        "  tabsize=4,\n"
        "  xleftmargin=12pt,\n"
        "  xrightmargin=4pt,\n"
        "  extendedchars=false,\n"
        "}\n"
        "\\lstset{style=pythonstyle}\n"
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


# ─── Image & Claude Helpers ───────────────────────────────────────────────────

def image_to_base64(path: str) -> tuple[str, str]:
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }
    media_type = ext_map.get(Path(path).suffix.lower(), "image/jpeg")
    with open(path, "rb") as file_handle:
        return base64.standard_b64encode(file_handle.read()).decode(), media_type


def pdf_to_base64(path: str) -> str:
    """Read a PDF file and return its base64-encoded content."""
    with open(path, "rb") as file_handle:
        return base64.standard_b64encode(file_handle.read()).decode()


def process_image(client: anthropic.Anthropic, image_path: str, usage: Usage) -> str:
    """Send image to Claude via streaming: extract question + return structured answer."""
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


# ─── Misc Helpers ─────────────────────────────────────────────────────────────

def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {seconds % 60:.1f}s"


def _print_usage_summary(usage: Usage) -> None:
    print(f"\n── Token Usage ──────────────────────────────────────────")
    print(usage.summary())


# ─── Python Exam System Prompt ────────────────────────────────────────────────

_PYTHON_ANSWER_SYSTEM = (
    "You are a Python programming teacher for Thai middle and high school students "
    "(secondary school, ages 12–18).\n\n"

    "The user will send you a PDF of a Python programming exam question. "
    "Solve it completely AND teach the student WHY it works — "
    "like a patient, encouraging teacher explaining to a friend.\n\n"

    "Respond in Thai except for: Python code, function/variable names, "
    "and standard technical terms (loop, function, list, index, etc.).\n\n"

    "Use simple, friendly Thai language a Mathayom student can understand. "
    "Avoid jargon; if you must use a term, explain it briefly.\n\n"

    "Structure your response using EXACTLY these markdown headings "
    "(no additions, no renames):\n\n"

    "## โจทย์\n"
    "อ่านโจทย์จาก PDF แล้วสรุปให้ชัดเจน: โปรแกรมต้องรับ input อะไร "
    "และต้องให้ output อะไร อธิบายเป็น 1–2 ประโยค\n\n"

    "## แนวคิด\n"
    "อธิบายแนวคิดหลักที่ใช้แก้โจทย์นี้เป็นภาษาไทย:\n"
    "- เราต้องทำอะไรกับ input\n"
    "- ใช้เครื่องมืออะไรใน Python (loop / if-else / list / function) และทำไม\n"
    "- มีสูตรหรือ algorithm พิเศษไหม\n"
    "สามารถให้ข้อมูลเพิ่มเติมในส่วนนี้ได้โดยไม่จำกัดความยาว เพื่อความครบถ้วนของเนื้อหา\n\n"

    "## Python Code\n"
    "เขียน Python 3 code ที่ถูกต้อง สมบูรณ์ และอ่านง่าย\n"
    "กฎสำคัญ (ต้องทำตามเสมอ):\n"
    "  - ใช้ if __name__ == \"__main__\":: เพื่อสอนรูปแบบมาตรฐานของ Python ในการรันไฟล์ และป้องกันไม่ให้ Code ใน main() รันโดยไม่ตั้งใจเมื่อมีการ import ไปใช้ในอนาคต\n"
    "  - แบ่งฟังก์ชันตามหน้าที่ (Modularization): แยกการคำนวณ (Logic) ออกจากส่วนแสดงผล (UI/Print) ให้ชัดเจน เพื่อให้นักเรียนเห็นภาพว่าฟังก์ชันหนึ่งควรทำหน้าที่เพียงอย่างเดียว"
    "  - เลือกวิธีคำนวนที่เข้าใจง่ายที่สุด ไม่ต้องกังวลเรื่องประสิทธิภาพ เนื่องจากเป้าหมายคือการสอน\n"
    "  - หลีกเลี่ยงเทคนิคขั้นสูงที่ซับซ้อนเกินไปสำหรับนักเรียนระดับนี้ (เช่น list comprehensions, lambda, decorators) ยกเว้นว่าจำเป็นจริงๆ\n"
    "  - ใช้ Type Hinting พื้นฐาน: แม้จะเป็นมือใหม่ แต่การระบุชนิดตัวแปร เช่น def add_numbers(a: int, b: int) -> int: จะช่วยให้นักเรียนเข้าใจว่าฟังก์ชันรับและคืนค่าอะไร โดยไม่ต้องไล่ Code นาน"
    "  - อนุญาติให้ใช้ recursive functions ได้ แต่ต้องอธิบายแนวคิดการทำงานของ recursive ในส่วน แนวคิด ด้วย\n"
    "  - รับ input ผ่านฟังก์ชัน input() และพิมพ์ output ด้วย print() เท่านั้น\n"
    "  - ใช้เฉพาะ ASCII ใน code (ไม่มีภาษาไทยในตัว code, comment, หรือ string ใดๆ)\n"
    "  - Comment ใน code ต้องเป็น English เท่านั้น: # like this\n"
    "  - ตั้งชื่อตัวแปรเป็น English ที่สื่อความหมาย\n"
    "  - Step-by-Step Comments: ในฟังก์ชันที่ซับซ้อน ให้ใส่ Comment อธิบายทีละขั้นตอน (Step 1, Step 2) แทนการอธิบายรวบยอดครั้งเดียว เพื่อให้นักเรียนเห็นภาพกระบวนการคิด\n"
    "  - แสดง expected output เป็น comment ท้าย code: # Output: 42\n"
    "ครอบ code ด้วย ```python ... ```\n\n"

    "## อธิบายโค้ด\n"
    "อธิบายโค้ดทีละส่วนเป็นภาษาไทยละเอียด เหมือนสอนเพื่อนที่เพิ่งเริ่มเรียน Python:\n"
    "- บอกว่าแต่ละ line หรือ block ทำอะไร\n"
    "- บอกว่าทำไมถึงเขียนแบบนี้ ไม่ใช่แบบอื่น\n"
    "- ถ้ามีแนวคิดพิเศษ (เช่น modulo %, integer division //, index) "
    "ให้ยกตัวอย่างง่ายๆ ประกอบด้วย\n"
    "Trace Table (สำหรับ Recursive): หากมีการใช้ Recursive การแสดงตารางการทำงานของ Stack (เรียกฟังก์ชันซ้อนกันอย่างไร) จะช่วยให้เด็กเห็นภาพมากกว่าคำบรรยาย"
    "ใช้หมายเลขข้อ: 1. 2. 3. ...\n\n"

    "## ตรวจสอบ\n"
    "CRITICAL — ห้ามข้ามขั้นตอนนี้เด็ดขาด ก่อนส่งคำตอบต้องทำ dry-run ดังนี้:\n"
    "1. เลือก sample input จากโจทย์ (หรือสร้างขึ้นถ้าโจทย์ไม่ให้)\n"
    "2. Trace ผ่านโค้ดทีละบรรทัด แสดงค่าตัวแปรแต่ละขั้นตอน\n"
    "3. ระบุ output ที่ได้จากการ trace\n"
    "4. เปรียบเทียบกับ expected output จากโจทย์\n"
    "5. ถ้า output ไม่ตรง → แก้โค้ดใน section Python Code แล้ว trace ใหม่จนถูก\n"
    "แสดงการ trace ในรูปตาราง: | บรรทัด | ตัวแปร | ค่า |\n\n"

    "---\n"
    "Math formatting (for explanation sections only — NOT inside code):\n"
    "- Inline: $n^2 + 1$\n"
    "- Display: $$\\frac{n(n+1)}{2}$$\n"
    "- Always use LaTeX commands, never Unicode math symbols (×, ², ≤, etc.)\n"
    "- Never use emoji or decorative symbols anywhere in the response."
)


# ─── Python PDF Builders ──────────────────────────────────────────────────────

def parse_python_sections(text: str) -> dict[str, str]:
    headings = ["โจทย์", "แนวคิด", "Python Code", "อธิบายโค้ด", "ตรวจสอบ", "คำตอบ"]
    escaped = [re.escape(h) for h in headings]
    pattern = (
        r"##\s+(" + "|".join(escaped) + r")\s*\n"
        r"(.*?)(?=##\s+(?:" + "|".join(escaped) + r")|$)"
    )
    return {
        m.group(1).strip(): m.group(2).strip()
        for m in re.finditer(pattern, text, re.DOTALL)
    }


def extract_python_code(answer: str) -> str | None:
    """Extract the raw Python code from the ## Python Code section."""
    sections = parse_python_sections(answer)
    code_section = sections.get("Python Code", "")
    m = re.search(r"```python\s*\n(.*?)```", code_section, re.DOTALL)
    if m:
        return m.group(1).rstrip()
    m = re.search(r"```\w*\s*\n(.*?)```", code_section, re.DOTALL)
    if m:
        return m.group(1).rstrip()
    return code_section.strip() or None


def build_python_preamble(fonts_path: str, reg_stem: str, bold_stem: str) -> str:
    return build_preamble(fonts_path, reg_stem, bold_stem) + (
        "\n"
        "\\newtcolorbox{conceptenv}{\n"
        "  enhanced, breakable,\n"
        "  colback=green!5, colframe=secgreen,\n"
        "  left=8pt, right=8pt, top=6pt, bottom=6pt, arc=4pt\n"
        "}\n"
        "\n"
        "\\newtcolorbox{explainenv}{\n"
        "  enhanced, breakable,\n"
        "  colback=purple!5, colframe=accent,\n"
        "  left=8pt, right=8pt, top=6pt, bottom=6pt, arc=4pt\n"
        "}\n"
        "\n"
        "\\newtcolorbox{verifyenv}{\n"
        "  enhanced, breakable,\n"
        "  colback=yellow!8, colframe=answer,\n"
        "  left=8pt, right=8pt, top=6pt, bottom=6pt, arc=4pt\n"
        "}\n"
    )


def build_python_tex(sections: dict[str, str], timestamp: str) -> str:
    problem_body = md_to_latex(sections.get("โจทย์", ""))
    concept_body = md_to_latex(sections.get("แนวคิด", ""))
    code_body = md_to_latex(sections.get("Python Code", ""))
    explain_body = md_to_latex(sections.get("อธิบายโค้ด", ""))
    verify_body = md_to_latex(sections.get("ตรวจสอบ", ""))

    verify_section = (
        "\n"
        "\\vspace{6pt}\n"
        "\\sectbox{5.\\ ตรวจสอบ (Verification)}\n"
        "\\begin{verifyenv}\n"
        + verify_body + "\n"
        "\\end{verifyenv}\n"
    ) if verify_body.strip() else ""

    return (
        "\\input{preamble.tex}\n"
        "\\begin{document}\n"
        "\n"
        "\\begin{center}\n"
        "  {\\Huge\\bfseries\\color{primary}Python Answer}\\\\[6pt]\n"
        "  {\\large\\color{muted}\\textenglish{QA Claude \\quad " + timestamp + "}}\n"
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
        "\\begin{conceptenv}\n"
        + concept_body + "\n"
        "\\end{conceptenv}\n"
        "\n"
        "\\vspace{6pt}\n"
        "\\sectbox{3.\\ Python Code}\n"
        + code_body + "\n"
        "\n"
        "\\vspace{6pt}\n"
        "\\sectbox{4.\\ อธิบายโค้ด (Code Explanation)}\n"
        "\\begin{explainenv}\n"
        + explain_body + "\n"
        "\\end{explainenv}\n"
        + verify_section
        + "\n"
        "\\end{document}\n"
    )


def _write_python_latex_files(
    build_dir: Path, answer: str, fonts_path: str, reg_stem: str, bold_stem: str
) -> None:
    timestamp = datetime.now().strftime("%d %B %Y, %H:%M")
    sections = parse_python_sections(answer)
    (build_dir / "preamble.tex").write_text(
        build_python_preamble(fonts_path, reg_stem, bold_stem), encoding="utf-8"
    )
    (build_dir / "output.tex").write_text(build_python_tex(sections, timestamp), encoding="utf-8")


def create_python_pdf(answer: str, output_path: str, xelatex_path: str | None = None) -> None:
    if xelatex_path is None:
        xelatex_path = _find_xelatex()
    if not xelatex_path:
        raise RuntimeError("xelatex not found — install MiKTeX (Windows) or texlive-xetex (Linux)")

    print("\n📄  Resolving fonts...")
    fonts_path, reg_stem, bold_stem = resolve_fonts()

    build_dir = Path(tempfile.mkdtemp(prefix="qalatex_py_"))
    print(f"📄  Build dir: {build_dir}")

    _write_python_latex_files(build_dir, answer, fonts_path, reg_stem, bold_stem)

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
