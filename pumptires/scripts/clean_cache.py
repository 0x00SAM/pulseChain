#!/usr/bin/env python3
"""Cache cleanup utility for the Pump Tires monitor project.

The script removes common Python cache artefacts (``__pycache__`` directories,
``*.pyc`` files, mypy and pytest caches, build outputs, and similar items) so
local development runs start from a clean slate. Optionally, it can also remove
files from the application's log directory.

Examples::

    python scripts/clean_cache.py             # Clean Python caches (default)
    python scripts/clean_cache.py --dry-run   # Preview without deleting
    python scripts/clean_cache.py --logs      # Clean logs only
    python scripts/clean_cache.py --all       # Clean caches and logs

Always perform a dry run first if you are unsure about what will be removed.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
from pathlib import Path

CACHE_DIRECTORY_PATTERNS: tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "build",
    "dist",
    ".ipynb_checkpoints",
)
CACHE_FILE_PATTERNS: tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.so",
    "*.dll",
    "*.dylib",
    "*.egg-info",
    ".coverage",
    ".coverage.*",
)
TOP_LEVEL_SKIP_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".direnv",
    "node_modules",
)


class CacheCleanup:
    """Handles cleanup of Python cache artefacts and application logs."""

    def __init__(
        self,
        project_root: Path,
        *,
        dry_run: bool = False,
        verbose: bool = False,
        log_path: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.dry_run = dry_run
        self.verbose = verbose
        self.log_path = log_path.resolve() if log_path else None
        self.total_size = 0
        self.deleted_count = 0

    def _is_relative_to(self, path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    def _is_allowed_target(self, item_path: Path) -> bool:
        resolved = item_path.resolve()
        if self._is_relative_to(resolved, self.project_root):
            return True
        if self.log_path and self._is_relative_to(resolved, self.log_path):
            return True
        if self.verbose:
            print(f"Skipping outside project scope: {resolved}")
        return False

    def get_directory_size(self, path: Path) -> int:
        """Calculate the total size of a directory."""

        total_size = 0
        try:
            for dirpath, _, filenames in os.walk(path):
                for filename in filenames:
                    filepath = Path(dirpath) / filename
                    try:
                        total_size += filepath.stat().st_size
                    except (OSError, FileNotFoundError):
                        continue
        except (OSError, FileNotFoundError):
            return 0
        return total_size

    def format_size(self, size_bytes: int) -> str:
        """Format size in bytes to a human-readable string."""

        if size_bytes <= 0:
            return "0.0 B"
        amount = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024:
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{amount:.1f} TB"

    def remove_item(self, item_path: Path) -> None:
        """Remove a file or directory, respecting dry-run and verbosity."""

        if not self._is_allowed_target(item_path):
            return

        try:
            if item_path.is_dir():
                size = self.get_directory_size(item_path)
                if self.dry_run:
                    if self.verbose:
                        print(
                            f"Would remove directory: {item_path}"
                            f" ({self.format_size(size)})"
                        )
                else:
                    shutil.rmtree(item_path)
                    if self.verbose:
                        print(
                            f"Removed directory: {item_path}"
                            f" ({self.format_size(size)})"
                        )
            else:
                size = item_path.stat().st_size if item_path.exists() else 0
                if self.dry_run:
                    if self.verbose:
                        print(
                            f"Would remove file: {item_path}"
                            f" ({self.format_size(size)})"
                        )
                else:
                    item_path.unlink(missing_ok=True)
                    if self.verbose:
                        print(
                            f"Removed file: {item_path}"
                            f" ({self.format_size(size)})"
                        )

            self.total_size += size
            self.deleted_count += 1
        except (OSError, PermissionError) as exc:
            print(f"Error removing {item_path}: {exc}", file=sys.stderr)

    def find_cache_items(self) -> list[Path]:
        """Find all Python cache files and directories."""

        items_to_clean: list[Path] = []

        for root, dirs, files in os.walk(self.project_root):
            root_path = Path(root)

            if root_path == self.project_root:
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in TOP_LEVEL_SKIP_DIRS or d in CACHE_DIRECTORY_PATTERNS
                ]

            for dir_name in list(dirs):
                if any(fnmatch.fnmatch(dir_name, pattern) for pattern in CACHE_DIRECTORY_PATTERNS):
                    items_to_clean.append(root_path / dir_name)
                    dirs.remove(dir_name)

            for file_name in files:
                if any(fnmatch.fnmatch(file_name, pattern) for pattern in CACHE_FILE_PATTERNS):
                    items_to_clean.append(root_path / file_name)

        return sorted(set(items_to_clean))

    def clean_python_cache(self) -> None:
        """Perform the Python cache cleanup."""

        print(f"Scanning for Python cache artefacts in: {self.project_root}")
        cache_items = self.find_cache_items()

        if not cache_items:
            print("No Python cache items found to clean.")
            return

        print(f"Found {len(cache_items)} cache items to clean:")
        for item in cache_items:
            self.remove_item(item)

    def clean_log_directory(self) -> None:
        """Clean all files from the specified log directory."""

        if not self.log_path:
            print("\nLog directory not specified; skipping log cleanup.")
            return

        if not self._is_relative_to(self.log_path, self.project_root):
            print(
                f"\nSkipping log cleanup; path outside project root: {self.log_path}",
                file=sys.stderr,
            )
            return

        if not self.log_path.exists() or not self.log_path.is_dir():
            print(f"\nLog directory not found: {self.log_path}", file=sys.stderr)
            return

        print(f"\nCleaning application logs in: {self.log_path}")

        try:
            log_files = [file for file in self.log_path.iterdir() if file.is_file()]
        except PermissionError as exc:
            print(f"Error accessing log directory {self.log_path}: {exc}", file=sys.stderr)
            print(
                "Hint: you may need elevated permissions if cleaning system-level logs.",
                file=sys.stderr,
            )
            return

        if not log_files:
            print("No log files found to clean.")
            return

        print(f"Found {len(log_files)} log files to clean:")
        for log_file in log_files:
            self.remove_item(log_file)

    def run(self, clean_cache: bool, clean_logs: bool) -> None:
        """Perform the cleanup based on the requested options."""

        print("Starting cleanup...")
        if self.dry_run:
            print("DRY RUN MODE - no files will be deleted\n")

        if clean_cache:
            self.clean_python_cache()

        if clean_logs:
            self.clean_log_directory()

        summary_prefix = "Summary (DRY RUN):" if self.dry_run else "Summary:"
        print(f"\n{summary_prefix}")
        print(f"  Items {'inspected' if self.dry_run else 'processed'}: {self.deleted_count}")
        print(
            f"  Total size {'that would be freed' if self.dry_run else 'freed'}: "
            f"{self.format_size(self.total_size)}"
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean Python cache files/directories and optional log files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:\n  python scripts/clean_cache.py --dry-run\n  python scripts/clean_cache.py --all --verbose\n        """,
    )

    parser.add_argument(
        "--cache",
        action="store_true",
        help="Clean only Python cache files (default when no flags are provided).",
    )
    parser.add_argument(
        "--logs",
        action="store_true",
        help="Clean only application log files.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Clean both cache artefacts and application logs.",
    )
    parser.add_argument(
        "--log-path",
        type=str,
        default="logs",
        help="Path to the application log directory (relative to project root).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without removing any files.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output of what is being processed.",
    )

    return parser.parse_args()


def resolve_project_root(script_path: Path) -> Path:
    """Return the project root directory given the script location."""

    return script_path.resolve().parent.parent


def resolve_log_path(raw_path: str | None, project_root: Path) -> Path | None:
    """Resolve the provided log path relative to the project root."""

    if not raw_path:
        return None

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate

    candidate = candidate.resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        print(
            "Log path must reside within the project root; ignoring provided value.",
            file=sys.stderr,
        )
        return None

    if candidate == project_root:
        print(
            "Log path resolves to the project root; refusing to clean entire project.",
            file=sys.stderr,
        )
        return None

    return candidate


def main() -> None:
    args = parse_arguments()

    clean_cache = args.all or args.cache or not (args.cache or args.logs or args.all)
    clean_logs = args.all or args.logs

    project_root = resolve_project_root(Path(__file__))
    log_path = resolve_log_path(args.log_path, project_root) if args.log_path else None

    cleaner = CacheCleanup(
        project_root=project_root,
        dry_run=args.dry_run,
        verbose=args.verbose,
        log_path=log_path if clean_logs else None,
    )

    try:
        cleaner.run(clean_cache=clean_cache, clean_logs=clean_logs)
    except KeyboardInterrupt:
        print("\nCleaning interrupted by user.")
        sys.exit(1)
    except Exception as exc:  # pragma: no cover - unexpected failure guard
        print(f"\nAn unexpected error occurred: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()