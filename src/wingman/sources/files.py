# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Local files you provide: a LinkedIn 'Save to PDF' export, a CV (.pdf, .md, .txt)."""

from __future__ import annotations

import re
from pathlib import Path

from wingman.core.models import Document

MAX_CHARS = 30000


def read_file(path: Path) -> Document:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    else:
        text = path.read_text(errors="replace")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError(f"No text found in {path.name} (scanned image PDF?)")
    return Document(title=path.name, text=text[:MAX_CHARS], kind="file")
