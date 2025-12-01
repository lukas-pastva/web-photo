# app.py
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, send_file, abort, jsonify
import os
import logging
import ffmpeg
import shutil
from PIL import Image, ImageOps
import pyheif
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import io
import zipfile
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
import json
from script_manager import ScriptManager
from tasks import rebuild_previews_task

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configurable upload directory via environment variable
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Encourage browser caching of served files (uploads and static)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 30  # 30 days

# Configurable image quality via environment variable
# Default quality is 100
IMAGE_QUALITY = int(os.environ.get('IMAGE_QUALITY', '100'))

# Configurable thumbnail quality (percentage)
THUMBNAIL_QUALITY = int(os.environ.get('THUMBNAIL_QUALITY', '85'))


# Set maximum upload size to 5GB (adjust as needed)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024  # 5 GB

# Secret key for CSRF protection
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key')

# Ensure the upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'heic', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'mp4', 'mov', 'avi', 'mkv', 'm4v'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Where long-running script state (logs, progress) is stored.
STATE_DIR = os.environ.get('STATE_DIR', os.path.join(app.config['UPLOAD_FOLDER'], '.state'))
os.makedirs(STATE_DIR, exist_ok=True)

def rebuild_previews_runner(ctx, category=None):
    """Admin-triggered runner that supports stop/resume."""
    rebuild_previews_task(
        app=app,
        process_file=process_file,
        allowed_file=allowed_file,
        category=category or None,
        progress=ctx.progress,
        progress_key=ctx.job.progress_key,
        stop_event=ctx.job.stop_event,
        logger=ctx.log,
    )

MANAGED_SCRIPTS = {
    "rebuild_previews": {
        "label": "Rebuild previews",
        "description": "Regenerate derived images and metadata. Resumes by skipping processed files.",
        "runner": rebuild_previews_runner,
        "params": {
            "category": {
                "label": "Category (leave blank for all)",
                "required": False,
            }
        },
        "progress_key_fn": lambda params: f"rebuild_previews:{params.get('category') or 'all'}",
    },
}

script_manager = ScriptManager(state_dir=STATE_DIR, scripts=MANAGED_SCRIPTS)

def process_file(filepath, category):
    filename = os.path.basename(filepath)
    name, ext = os.path.splitext(filename)
    ext = ext.lower()

    # Define image and video extensions
    image_extensions = {'.heic', '.jpg', '.jpeg', '.png', '.gif', '.bmp'}
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.m4v'}

    if ext not in image_extensions and ext not in video_extensions:
        # Unsupported file type
        logger.warning(f"Unsupported file type: {filename}")
        return

    if ext in video_extensions:
        thumb_source_path = filepath
        # If .m4v, convert to .mp4 first
        if ext == '.m4v':
            source_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
            dest_dir = source_dir
            mp4_filename = f"{name}.mp4"
            mp4_filepath = os.path.join(dest_dir, mp4_filename)
            try:
                logger.info(f"Converting {filename} to {mp4_filename}")
                (
                    ffmpeg
                    .input(filepath)
                    .output(mp4_filepath, vcodec='libx264', acodec='aac', strict='experimental')
                    .overwrite_output()
                    .run()
                )
                os.remove(filepath)
                logger.info(f"Successfully converted {filename} to {mp4_filename}")
                thumb_source_path = mp4_filepath
            except ffmpeg.Error as e:
                logger.error(f"Error converting {filename}: {e.stderr.decode()}")
                return

        # Generate video poster thumbnail (JPEG)
        try:
            thumb_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'video_thumbnail')
            os.makedirs(thumb_dir, exist_ok=True)
            thumb_path = os.path.join(thumb_dir, f'{name}.jpeg')
            logger.info(f"Generating video thumbnail for {os.path.basename(thumb_source_path)} -> {thumb_path}")
            (
                ffmpeg
                .input(thumb_source_path, ss=1)
                .filter('scale', 400, -1)
                .output(thumb_path, vframes=1)
                .overwrite_output()
                .run()
            )
        except ffmpeg.Error as e:
            logger.error(f"Error generating video thumbnail for {filename}: {getattr(e, 'stderr', b'').decode(errors='ignore')}")
        except Exception as e:
            logger.error(f"Unexpected error generating video thumbnail for {filename}: {str(e)}")
        # Do not process further as image
        return

    # Handle image files
    try:
        if ext == '.heic':
            logger.info(f"Processing HEIC image: {filename}")
            heif_file = pyheif.read(filepath)
            image = Image.frombytes(
                heif_file.mode,
                heif_file.size,
                heif_file.data,
                "raw",
                heif_file.mode,
                heif_file.stride,
            )
            # Convert HEIC to JPEG
            original_format = 'JPEG'
            save_extension = '.jpeg'
        else:
            logger.info(f"Processing image: {filename}")
            image = Image.open(filepath)
            original_format = image.format  # Get the original image format
            save_extension = ext  # Use the original file extension

        # Normalize orientation based on EXIF so saved images display correctly
        try:
            image = ImageOps.exif_transpose(image)
        except Exception as _e:
            # If anything goes wrong, continue without transposition
            pass

        # Handle images with transparency (alpha channel)
        if image.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])  # Use alpha channel as mask
            image = background  # Update the image variable to the new image without alpha
            original_format = 'JPEG'  # Save as JPEG since transparency is removed
            save_extension = '.jpeg'
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        # Define the sizes for different resolutions
        sizes = {
            'largest': (2880, 1620),
            'medium': (1920, 1080),
            'thumbnail': (400, 400),
        }

        # Generate and save images in different resolutions
        # Also capture dimensions for each saved size to persist
        saved_dimensions = {}
        for size_name, size in sizes.items():
            img_copy = image.copy()
            if size_name == 'largest':
                # Only resize if the image is larger than the target size
                if image.width > size[0] or image.height > size[1]:
                    img_copy.thumbnail(size)
                # Save the image in the appropriate format with maximum quality
                save_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, size_name)
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, f'{name}{save_extension}')
                # Use progressive/optimized JPEG when applicable for faster progressive rendering
                if (original_format or '').upper() == 'JPEG' or save_path.lower().endswith(('.jpg', '.jpeg')):
                    img_copy.save(save_path, 'JPEG', quality=100, optimize=True, progressive=True)
                else:
                    img_copy.save(save_path, original_format)
                logger.info(f"Saved largest image: {save_path}")
                saved_dimensions[size_name] = {'width': img_copy.width, 'height': img_copy.height}
            elif size_name == 'medium':
                # Resize to the target size
                img_copy.thumbnail(size)
                # Save as JPEG with default image quality
                save_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, size_name)
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, f'{name}.jpeg')
                img_copy.save(save_path, 'JPEG', quality=IMAGE_QUALITY, optimize=True, progressive=True)
                logger.info(f"Saved medium image: {save_path}")
                saved_dimensions[size_name] = {'width': img_copy.width, 'height': img_copy.height}
            elif size_name == 'thumbnail':
                # Resize to the target size
                img_copy.thumbnail(size)
                # Save as JPEG with reduced quality
                save_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, size_name)
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, f'{name}.jpeg')
                img_copy.save(save_path, 'JPEG', quality=THUMBNAIL_QUALITY, optimize=True, progressive=True)
                logger.info(f"Saved thumbnail image: {save_path}")
                saved_dimensions[size_name] = {'width': img_copy.width, 'height': img_copy.height}

        # Persist dimensions for this image so PhotoSwipe can use correct aspect ratio
        try:
            dimensions_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'dimensions.json')
            os.makedirs(os.path.dirname(dimensions_path), exist_ok=True)
            if os.path.exists(dimensions_path):
                with open(dimensions_path, 'r') as f:
                    dims_data = json.load(f)
            else:
                dims_data = {}
            dims_data[name] = saved_dimensions
            with open(dimensions_path, 'w') as f:
                json.dump(dims_data, f)
            logger.info(f"Updated dimensions metadata: {dimensions_path}")
        except Exception as e:
            logger.warning(f"Could not write dimensions metadata for {filename}: {e}")

    except Exception as e:
        logger.error(f"Error processing image {filename}: {str(e)}")

def build_tree_data(categories):
    tree = {}
    for category in categories:
        parts = category.split('-')
        current_level = tree
        for part in parts:
            if part not in current_level:
                current_level[part] = {}
            current_level = current_level[part]
    def build_nodes(current_dict, parent_path=''):
        nodes = []
        for name, subtree in sorted(current_dict.items()):
            full_path = f"{parent_path}-{name}" if parent_path else name
            node = {
                'text': name,  # Removed the delete icon
                'href': url_for('category_view', category=full_path),
                'selectable': True
            }
            if subtree:
                node['nodes'] = build_nodes(subtree, full_path)
            nodes.append(node)
        return nodes
    return build_nodes(tree)

class CategoryForm(FlaskForm):
    category_name = StringField('Category Name', validators=[DataRequired()])
    submit = SubmitField('Create Category')

@app.route('/')
def index():
    categories = os.listdir(app.config['UPLOAD_FOLDER'])
    form = CategoryForm()
    
    # Filter only directories
    categories = [c for c in categories if os.path.isdir(os.path.join(app.config['UPLOAD_FOLDER'], c))]
    
    # Build tree data based on category names with "-" as hierarchy
    treeData = build_tree_data(categories)
    
    return render_template('index.html', categories=categories, form=form, treeData=treeData)

@app.route('/admin')
def admin_dashboard():
    scripts_payload = []
    for name, meta in MANAGED_SCRIPTS.items():
        scripts_payload.append({
            'name': name,
            'label': meta.get('label', name),
            'description': meta.get('description', ''),
            'params': meta.get('params', {}),
        })
    jobs = script_manager.list_jobs()
    # Compute processed counts for display
    for job in jobs:
        job['processed_count'] = script_manager.progress.count(job.get('progress_key', job['script']))
    return render_template('admin.html', scripts=scripts_payload, jobs=jobs)

@app.route('/admin/scripts/run', methods=['POST'])
def start_script():
    data = request.get_json(silent=True) or {}
    script_name = data.get('script')
    params = data.get('params') or {}
    if not script_name or script_name not in MANAGED_SCRIPTS:
        return jsonify({'error': 'Unknown script'}), 400
    try:
        job = script_manager.start_job(script_name, params)
    except (RuntimeError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(script_manager.job_status(job.id)), 200

@app.route('/admin/jobs', methods=['GET'])
def list_jobs_api():
    jobs = script_manager.list_jobs()
    for job in jobs:
        job['processed_count'] = script_manager.progress.count(job.get('progress_key', job['script']))
    return jsonify({'jobs': jobs})

@app.route('/admin/jobs/<job_id>/status', methods=['GET'])
def job_status(job_id):
    data = script_manager.job_status(job_id)
    if not data:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(data)

@app.route('/admin/jobs/<job_id>/log', methods=['GET'])
def job_log(job_id):
    try:
        offset = int(request.args.get('offset', 0))
    except ValueError:
        offset = 0
    log_data = script_manager.read_log(job_id, offset=offset)
    if log_data is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(log_data)

@app.route('/admin/jobs/<job_id>/stop', methods=['POST'])
def stop_job(job_id):
    if script_manager.stop_job(job_id):
        return jsonify({'status': 'stopping'})
    return jsonify({'error': 'Job not running'}), 400

@app.route('/category/<category>')
def category_view(category):
    # Define directories
    largest_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'largest')
    source_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
    dimensions_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'dimensions.json')

    images = []
    videos = []
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic'}
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv'}

    # Load dimensions
    if os.path.exists(dimensions_path):
        with open(dimensions_path, 'r') as f:
            all_dimensions = json.load(f)
    else:
        all_dimensions = {}

    # Fetch images from 'largest' directory
    if os.path.exists(largest_dir):
        image_files = os.listdir(largest_dir)
        for file in image_files:
            name, ext = os.path.splitext(file)
            ext = ext.lower()
            if ext in image_extensions:
                # Prefer persisted dimensions; if missing, inspect the actual file
                width = all_dimensions.get(name, {}).get('largest', {}).get('width')
                height = all_dimensions.get(name, {}).get('largest', {}).get('height')
                if width is None or height is None:
                    try:
                        img_path = os.path.join(largest_dir, file)
                        with Image.open(img_path) as im:
                            width, height = im.size
                    except Exception:
                        # Fallback to a sane default if inspection fails
                        width, height = 1024, 768

                images.append({
                    'name': name,
                    'ext': ext,
                    'filename': file,
                    'width': width,
                    'height': height
                })

    # Fetch videos from 'source' directory
    if os.path.exists(source_dir):
        video_files = os.listdir(source_dir)
        for file in video_files:
            name, ext = os.path.splitext(file)
            ext = ext.lower()
            if ext in video_extensions:
                # Determine poster path (generated on upload)
                poster_rel = os.path.join(category, 'video_thumbnail', f'{name}.jpeg')
                poster_abs = os.path.join(app.config['UPLOAD_FOLDER'], poster_rel)
                if os.path.exists(poster_abs):
                    poster_url = url_for('uploaded_file', filename=poster_rel)
                else:
                    poster_url = url_for('static', filename='placeholder.jpg')

                videos.append({
                    'name': name,
                    'ext': ext,
                    'filename': file,
                    'poster': poster_url,
                    # Videos do not need width and height for PhotoSwipe
                })

    return render_template('category.html', category=category, images=images, videos=videos)

@app.route('/category/create', methods=['POST'])
def create_category():
    form = CategoryForm()
    if form.validate_on_submit():
        category = secure_filename(form.category_name.data)
        category_path = os.path.join(app.config['UPLOAD_FOLDER'], category)
        os.makedirs(category_path, exist_ok=True)
        # Create subdirectories
        for sub_dir in ['source', 'largest', 'medium', 'thumbnail']:
            os.makedirs(os.path.join(category_path, sub_dir), exist_ok=True)
    return redirect(url_for('index'))

@app.route('/category/delete/<category>', methods=['POST'])
def delete_category(category):
    category_path = os.path.join(app.config['UPLOAD_FOLDER'], category)
    if os.path.exists(category_path):
        try:
            shutil.rmtree(category_path)
            # If the request is AJAX, return JSON
            if request.is_json:
                return jsonify({'status': 'success', 'message': f"Category '{category}' has been deleted successfully."}), 200
            else:
                return redirect(url_for('index'))
        except Exception as e:
            if request.is_json:
                return jsonify({'status': 'fail', 'message': f"Error deleting category: {str(e)}"}), 500
            else:
                # Handle non-AJAX deletion if necessary
                return redirect(url_for('index'))
    else:
        if request.is_json:
            return jsonify({'status': 'fail', 'message': 'Category does not exist.'}), 404
        else:
            return redirect(url_for('index'))

@app.route('/upload/<category>', methods=['GET', 'POST'])
def upload_file(category):
    if request.method == 'POST':
        # Check if the post request has the file part
        if 'photos[]' not in request.files:
            return jsonify({'status': 'fail', 'message': 'No file part'}), 400
        files = request.files.getlist('photos[]')
        if not files or files[0].filename == '':
            return jsonify({'status': 'fail', 'message': 'No selected files'}), 400
        for file in files:
            if file and allowed_file(file.filename):
                # Use secure filename
                filename = secure_filename(file.filename)
                # Determine destination directory based on file type
                ext = os.path.splitext(filename)[1].lower()
                if ext in {'.mp4', '.mov', '.avi', '.mkv'}:
                    dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
                else:
                    dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
                os.makedirs(dest_dir, exist_ok=True)
                filepath = os.path.join(dest_dir, filename)
                file.save(filepath)

                # Process the file
                process_file(filepath, category)
        return jsonify({'status': 'success', 'message': 'Files uploaded successfully.'}), 200
    form = CategoryForm()
    return render_template('upload.html', category=category, form=form)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, conditional=True)

@app.route('/download_category/<category>')
def download_category(category):
    # Get the size parameter from the query string
    size = request.args.get('size', 'largest')

    # Validate the size parameter
    valid_sizes = ['source', 'largest', 'medium']
    if size not in valid_sizes:
        return jsonify({'status': 'fail', 'message': 'Invalid size parameter.'}), 400

    # Path to the directory containing the images of the specified size
    images_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, size)
    if not os.path.exists(images_dir):
        return jsonify({'status': 'fail', 'message': 'Size not found.'}), 404

    # Collect all image file paths excluding videos
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv'}
    image_filenames = [f for f in os.listdir(images_dir) if os.path.splitext(f)[1].lower() not in video_extensions]
    image_paths = [os.path.join(images_dir, filename) for filename in image_filenames]

    if not image_filenames:
        return jsonify({'status': 'fail', 'message': 'No files to download.'}), 404

    # Create a ZIP file in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, filename in zip(image_paths, image_filenames):
            zip_file.write(file_path, arcname=filename)

    # Set the pointer to the beginning of the stream
    zip_buffer.seek(0)

    # Send the ZIP file as a response
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{category}_{size}_files.zip'
    )

@app.route('/download_videos/<category>')
def download_videos(category):
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv'}
    # Path to the 'source' directory for videos
    source_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
    if not os.path.exists(source_dir):
        return jsonify({'status': 'fail', 'message': 'Category not found.'}), 404

    # Filter video files
    video_filenames = [f for f in os.listdir(source_dir) if os.path.splitext(f)[1].lower() in video_extensions]
    video_paths = [os.path.join(source_dir, f) for f in video_filenames]

    if not video_filenames:
        return jsonify({'status': 'fail', 'message': 'No videos to download.'}), 404

    # Create a ZIP file in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, filename in zip(video_paths, video_filenames):
            zip_file.write(file_path, arcname=filename)

    # Set the pointer to the beginning of the stream
    zip_buffer.seek(0)

    # Send the ZIP file as a response
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{category}_videos.zip'
    )

@app.route('/delete_photo/<category>/<filename>', methods=['POST'])
def delete_photo(category, filename):
    # Define all sizes to delete
    sizes = ['source', 'largest', 'medium', 'thumbnail']
    success = True
    messages = []

    for size in sizes:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], category, size, filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                success = False
                messages.append(f'Error deleting {size} version: {str(e)}')

    # If deleting a video, also remove its generated poster
    name, ext = os.path.splitext(filename)
    video_extensions = {'.mp4', '.mov', '.avi', '.mkv'}
    if ext.lower() in video_extensions:
        poster_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'video_thumbnail', f'{name}.jpeg')
        if os.path.exists(poster_path):
            try:
                os.remove(poster_path)
            except Exception as e:
                success = False
                messages.append(f'Error deleting video thumbnail: {str(e)}')

    if success:
        return jsonify({'status': 'success', 'message': f"'{filename}' has been deleted successfully."}), 200
    else:
        return jsonify({'status': 'fail', 'message': ' '.join(messages)}), 500

@app.route('/download_single/<category>/<size>/<filename>')
def download_single(category, size, filename):
    # If the original file was m4v and converted to mp4, adjust the filename
    name, ext = os.path.splitext(filename)
    if ext.lower() == '.m4v' and size == 'source':
        filename = f"{name}.mp4"
    # Validate size
    valid_sizes = ['source', 'largest', 'medium']
    if size not in valid_sizes:
        abort(404)

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], category, size, filename)
    if not os.path.exists(file_path):
        abort(404)

    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename
    )

@app.errorhandler(RequestEntityTooLarge)
def handle_file_size_error(e):
    return jsonify({'status': 'fail', 'message': 'File too large. Maximum upload size is 5GB.'}), 413

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
