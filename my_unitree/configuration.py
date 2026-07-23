"""Project configuration discovery shared by installed command-line tools."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def find_project_root(start: Path | None = None) -> Path:
    """Find the repository from the current directory, not from site-packages."""
    configured_root = os.getenv("UNITREE_PROJECT_ROOT", "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    current = (start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "pyproject.toml").is_file():
        return source_root
    return current


def load_project_configuration(project_root: Path) -> None:
    """Load .env and locate the repository-local CycloneDDS installation."""
    load_dotenv(project_root / ".env")
    local_cyclonedds = project_root / ".deps" / "cyclonedds" / "install"
    if (
        not os.getenv("CYCLONEDDS_HOME", "").strip()
        and local_cyclonedds.is_dir()
    ):
        os.environ["CYCLONEDDS_HOME"] = str(local_cyclonedds)
