# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Local files you provide: a LinkedIn 'Save to PDF' export, a CV (.pdf, .docx, .md, .txt)."""

from __future__ import annotations

import re
from pathlib import Path

from wingman.core.models import Document

MAX_CHARS = 30000


def _docx_text(path: Path) -> str:
    """Word document -> Markdown-ish text: headings, bullets, paragraphs and table cells, in document order
    (resumes often lay out sections with tables)."""
    from docx import Document as WordDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def paragraph(p: Paragraph) -> str:
        text = p.text.strip()
        if not text:
            return ""
        style = (p.style.name if p.style is not None else "").lower()
        if style == "title":
            return f"# {text}"
        if style.startswith("heading"):
            level = int(style.rsplit(" ", 1)[-1]) if style[-1:].isdigit() else 1
            return f"{'#' * min(level + 1, 4)} {text}"
        if "list" in style or (p._p.pPr is not None and p._p.pPr.numPr is not None):
            return f"- {text}"
        return text

    def table(t: Table) -> list[str]:
        lines: list[str] = []
        for row in t.rows:
            seen: set[int] = set()
            for cell in row.cells:
                if id(cell._tc) in seen:  # merged cells repeat across the row
                    continue
                seen.add(id(cell._tc))
                lines += [line for p in cell.paragraphs if (line := paragraph(p))]
                for inner in cell.tables:
                    lines += table(inner)
        return lines

    doc = WordDocument(str(path))
    lines: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            line = paragraph(block)
            lines.append(line)
        else:
            lines += table(block)
    return "\n".join(lines)


def read_file(path: Path) -> Document:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    elif suffix == ".docx":
        text = _docx_text(path)
    elif suffix == ".doc":
        raise ValueError(f"{path.name} is an old Word .doc file: save it as .docx (or PDF) and point to that")
    else:
        text = path.read_text(errors="replace")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError(f"No text found in {path.name} (scanned image PDF?)")
    return Document(title=path.name, text=text[:MAX_CHARS], kind="file")
