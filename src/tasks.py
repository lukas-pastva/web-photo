import os
from typing import Callable, Optional


def rebuild_previews_task(
    app,
    process_file: Callable[[str, str], None],
    allowed_file: Callable[[str], bool],
    category: Optional[str] = None,
    progress=None,
    progress_key: Optional[str] = None,
    stop_event=None,
    logger: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Rebuild derived images (largest/medium/thumbnail) for one or all categories.
    Designed to be used both from the CLI helper and the admin runner.
    """
    log = logger or (lambda msg: None)
    progress_key = progress_key or f"rebuild_previews:{category or 'all'}"

    base = app.config["UPLOAD_FOLDER"]
    categories = [category] if category else [
        c for c in os.listdir(base) if os.path.isdir(os.path.join(base, c))
    ]

    for cat in categories:
        source_dir = os.path.join(base, cat, "source")
        if not os.path.isdir(source_dir):
            log(f"No source directory for category '{cat}' — skipping.")
            continue

        files = sorted(os.listdir(source_dir))
        total = len(files)
        if not total:
            log(f"No files in '{source_dir}' — nothing to do.")
            continue

        log(f"Rebuilding previews for category '{cat}' ({total} files)...")
        for idx, fname in enumerate(files, 1):
            if stop_event and stop_event.is_set():
                log("Stop requested. Exiting early.")
                return

            if not allowed_file(fname):
                continue

            item_key = f"{cat}/{fname}"
            if progress and progress.is_processed(progress_key, item_key):
                log(f"[{idx}/{total}] Skipping {item_key} (already processed)")
                continue

            path = os.path.join(source_dir, fname)
            try:
                process_file(path, cat)
                if progress:
                    progress.mark_processed(progress_key, item_key)
                log(f"[{idx}/{total}] Processed {item_key}")
            except Exception as exc:  # pragma: no cover - logged for operator visibility
                log(f"[{idx}/{total}] Failed {item_key}: {exc}")

    log("Rebuild task finished.")
