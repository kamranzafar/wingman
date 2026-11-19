# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Every Python file starts with the SPDX copyright and license header."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = "# SPDX-FileCopyrightText: 2026 Kamran Zafar\n# SPDX-License-Identifier: Apache-2.0\n"


def test_every_python_file_has_the_license_header():
    files = [*ROOT.joinpath("src").rglob("*.py"), *ROOT.joinpath("tests").rglob("*.py")]
    missing = [str(f.relative_to(ROOT)) for f in files if not f.read_text(encoding="utf-8").startswith(HEADER)]
    assert not missing, "Add the SPDX header (see CONTRIBUTING.md) to: " + ", ".join(missing)
