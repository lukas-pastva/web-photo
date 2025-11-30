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


def rebuild_for_category(category: str) -> None:
    base = app.config['UPLOAD_FOLDER']
    source_dir = os.path.join(base, category, 'source')
    if not os.path.isdir(source_dir):
        print(f"No source directory for category '{category}' — skipping.")
        return
    files = sorted(os.listdir(source_dir))
    total = len(files)
    if not total:
        print(f"No files in '{source_dir}' — nothing to do.")
        return
    print(f"Rebuilding previews for category '{category}' ({total} files)...")
    for i, fname in enumerate(files, 1):
        if not allowed_file(fname):
            continue
        path = os.path.join(source_dir, fname)
        try:
            process_file(path, category)
            print(f"[{i}/{total}] Processed {fname}")
        except Exception as e:
            print(f"[{i}/{total}] Failed {fname}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Rebuild image previews and orientation.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all', action='store_true', help='Rebuild all categories')
    group.add_argument('--category', help='Single category name to rebuild')
    args = parser.parse_args()

    base = app.config['UPLOAD_FOLDER']
    if args.all:
        categories = [c for c in os.listdir(base) if os.path.isdir(os.path.join(base, c))]
        for cat in sorted(categories):
            rebuild_for_category(cat)
    else:
        rebuild_for_category(args.category)


if __name__ == '__main__':
    main()

