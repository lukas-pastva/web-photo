# web-photo

A self-hosted photo and video gallery web application built with Flask. Upload photos and videos from any device, organize them into hierarchical categories, and browse them with auto-generated thumbnails and multiple image sizes.

Storage is backed by S3 (mounted via s3fs) so media files persist outside the container.

## Features

- **Photo upload & processing** -- upload images (JPEG, PNG, HEIC/HEIF, WebP, TIFF, BMP, DNG, GIF) from phone or desktop. Each photo is automatically resized into three variants: largest (2880x1620), medium (1920x1080), and thumbnail (400x400). EXIF orientation is normalized on upload.
- **Video upload & playback** -- upload videos (MP4, MOV, AVI, MKV, M4V, 3GP). M4V files are automatically remuxed to MP4 for browser compatibility. Thumbnails are generated from video frames via FFmpeg.
- **Hierarchical categories** -- organize media into nested categories using a dash-separated naming convention (e.g. `vacation-2024-italy`). Categories are displayed as a collapsible tree in the sidebar.
- **Admin dashboard** -- create, rename, move, and delete categories. Scan for and remove duplicate files across categories. Trigger background preview rebuilds.
- **Bulk download** -- download all photos or videos in a category as a ZIP archive, with selectable image size (source, largest, medium).
- **EXIF metadata display** -- camera model, date, ISO, aperture, shutter speed, focal length, lens, and GPS coordinates are extracted and shown in the gallery view.
- **Background task runner** -- long-running operations (e.g. rebuilding previews for all categories) run in background threads with progress tracking and log streaming.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `UPLOAD_FOLDER` | `uploads` | Base directory for all media storage |
| `IMAGE_QUALITY` | `100` | JPEG quality for medium-size images (1-100) |
| `THUMBNAIL_QUALITY` | `85` | JPEG quality for thumbnails (1-100) |
| `SECRET_KEY` | `your-secret-key` | Flask secret key for CSRF protection |

## Quick start (Docker)

```bash
docker build -f src/Dockerfile -t web-photo ./src
docker run -p 5000:5000 -v /path/to/photos:/tmp/uploads -e UPLOAD_FOLDER=/tmp/uploads web-photo
```

Open `http://localhost:5000` in your browser.

## Project structure

```
src/
  app.py               # Main Flask application (routes, image/video processing)
  tasks.py             # Reusable rebuild-previews task logic
  rebuild_previews.py  # CLI tool to rebuild derived images
  script_manager.py    # Background job runner with progress persistence
  requirements.txt     # Python dependencies
  Dockerfile           # Container image definition
  templates/           # Jinja2 HTML templates
  static/              # CSS, favicon, placeholder image
```

## Deploy to Kubernetes

The application is designed to run on Kubernetes with ArgoCD, using s3fs to mount an S3 bucket as the upload directory. Secrets (S3 credentials) are injected via HashiCorp Vault.

The CI pipeline (`.github/workflows/build.yaml`) builds a Docker image on every push to `main` and publishes it to GitHub Container Registry (`ghcr.io`).

## License

MIT