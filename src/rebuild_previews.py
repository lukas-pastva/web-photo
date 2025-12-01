#!/usr/bin/env python3
"""
Rebuild derived images (largest/medium/thumbnail) for one or all categories.
This normalizes orientation using EXIF and refreshes dimensions metadata.

Usage:
  python -m src.rebuild_previews --all
  python -m src.rebuild_previews --category my-category
"""
import argparse
import os
import sys

# Allow running as module or script
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from app import app, process_file, allowed_file  # noqa: E402
from tasks import rebuild_previews_task  # noqa: E402


def rebuild_for_category(category: str) -> None:
    rebuild_previews_task(
        app=app,
        process_file=process_file,
        allowed_file=allowed_file,
        category=category,
        logger=print,
    )


def main():
    parser = argparse.ArgumentParser(description="Rebuild image previews and orientation.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all', action='store_true', help='Rebuild all categories')
    group.add_argument('--category', help='Single category name to rebuild')
    args = parser.parse_args()

    if args.all:
        rebuild_previews_task(
            app=app,
            process_file=process_file,
            allowed_file=allowed_file,
            category=None,
            logger=print,
        )
    else:
        rebuild_for_category(args.category)


if __name__ == '__main__':
    main()
