# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Writes an application to disk: Markdown (what you edited) and DOCX (what you upload)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from wingman.jobs.models import Job

INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|`[^`]+`|\[[^\]]+\]\([^)]+\))")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "job"


def folder_for(root: Path, job: Job, today: date | None = None) -> Path:
    name = "-".join(p for p in ((today or date.today()).isoformat(), slug(job.company, 30), slug(job.title)) if p)
    return root / name


def _add_runs(paragraph, text: str) -> None:
    for part in INLINE.split(text):
        if not part:
            continue
        link = LINK.fullmatch(part)
        if link:
            label, url = link.groups()
            url = url.removeprefix("mailto:")
            paragraph.add_run(label if url in label else f"{label} ({url})")
        elif part[:2] in ("**", "__") and part[-2:] == part[:2] and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part[0] in "*_" and part[-1] == part[0] and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        elif part[0] == "`" and part[-1] == "`":
            paragraph.add_run(part[1:-1])
        else:
            paragraph.add_run(part)


def markdown_to_docx(markdown: str, path: Path) -> None:
    """Headings, bullet lists, paragraphs and **bold**/*italic*: what resumes and letters use."""
    from docx import Document
    from docx.shared import Cm, Pt

    doc = Document()
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Cm(2)
        section.left_margin = section.right_margin = Cm(2.2)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(4)

    paragraph_lines: list[str] = []

    def flush() -> None:
        if paragraph_lines:
            _add_runs(doc.add_paragraph(), " ".join(paragraph_lines))
            paragraph_lines.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if not stripped or re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
        elif heading:
            flush()
            level = len(heading.group(1)) - 1  # "#" is the document title (level 0)
            h = doc.add_heading(level=level)
            _add_runs(h, heading.group(2))
        elif bullet:
            flush()
            style = "List Bullet 2" if len(line) - len(line.lstrip()) >= 2 else "List Bullet"
            _add_runs(doc.add_paragraph(style=style), bullet.group(1))
        else:
            paragraph_lines.append(stripped)
    flush()
    doc.save(path)


def export(folder: Path, job: Job, resume_md: str, cover_letter_md: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for stem, text in (("resume", resume_md), ("cover-letter", cover_letter_md)):
        md = folder / f"{stem}.md"
        md.write_text(text.strip() + "\n", encoding="utf-8")
        docx = folder / f"{stem}.docx"
        markdown_to_docx(text, docx)
        files += [md, docx]
    ad = folder / "job.md"
    ad.write_text(f"# {job.title}\n\n{job.company} · {job.location}\n\n{job.url}\n\n{job.as_text()}\n",
                  encoding="utf-8")
    files.append(ad)
    return files
