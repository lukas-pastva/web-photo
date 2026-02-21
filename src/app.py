# app.py
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, send_file, abort, jsonify, Response, stream_with_context
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
IMAGE_QUALITY = int(os.environ.get('IMAGE_QUALITY', '100'))

# Configurable thumbnail quality (percentage)
THUMBNAIL_QUALITY = int(os.environ.get('THUMBNAIL_QUALITY', '85'))

# Set maximum upload size to 5GB
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024

# Secret key for CSRF protection
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key')

# Ensure the upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

IMAGE_EXTENSIONS = {'.heic', '.heif', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.dng', '.tiff', '.tif'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp'}
ALLOWED_EXTENSIONS = {e.lstrip('.') for e in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _list_categories():
    """Return sorted list of category directory names, excluding hidden dirs."""
    base = app.config['UPLOAD_FOLDER']
    return sorted(
        c for c in os.listdir(base)
        if os.path.isdir(os.path.join(base, c)) and not c.startswith('.')
    )


def _category_counts(categories):
    """Return dict mapping category name to photo and video counts."""
    base = app.config['UPLOAD_FOLDER']
    counts = {}
    for cat in categories:
        photos = 0
        videos = 0
        largest_dir = os.path.join(base, cat, 'largest')
        if os.path.isdir(largest_dir):
            for f in os.listdir(largest_dir):
                if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS:
                    photos += 1
        source_dir = os.path.join(base, cat, 'source')
        if os.path.isdir(source_dir):
            for f in os.listdir(source_dir):
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    videos += 1
        counts[cat] = {'photos': photos, 'videos': videos}
    return counts


def extract_photo_metadata(image):
    """Extract key EXIF metadata fields from a PIL Image for UI display."""
    meta = {}
    try:
        exif = image.getexif()
        if not exif:
            return meta

        # IFD0 tags
        make = str(exif.get(271, '')).strip()    # Make
        model = str(exif.get(272, '')).strip()   # Model
        if model:
            meta['camera'] = model if (make and make.lower() in model.lower()) else f"{make} {model}".strip()

        dt = exif.get(306)  # DateTime
        if dt:
            meta['date'] = str(dt)

        # EXIF Sub-IFD
        try:
            exif_ifd = exif.get_ifd(0x8769)
        except Exception:
            exif_ifd = {}

        if exif_ifd:
            dt_orig = exif_ifd.get(36867)  # DateTimeOriginal
            if dt_orig:
                meta['date'] = str(dt_orig)

            iso = exif_ifd.get(34855)  # ISOSpeedRatings
            if iso:
                try:
                    meta['iso'] = int(iso)
                except (TypeError, ValueError):
                    pass

            fnumber = exif_ifd.get(33437)  # FNumber
            if fnumber:
                try:
                    meta['aperture'] = f"f/{float(fnumber):.1f}"
                except (TypeError, ValueError):
                    pass

            exposure = exif_ifd.get(33434)  # ExposureTime
            if exposure:
                try:
                    exp_val = float(exposure)
                    if 0 < exp_val < 1:
                        meta['shutter'] = f"1/{int(round(1/exp_val))}"
                    else:
                        meta['shutter'] = f"{exp_val:.1f}s"
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

            focal = exif_ifd.get(37386)  # FocalLength
            if focal:
                try:
                    meta['focal_length'] = f"{float(focal):.1f}mm"
                except (TypeError, ValueError):
                    pass

            lens = exif_ifd.get(42036)  # LensModel
            if lens:
                meta['lens'] = str(lens)

        # GPS IFD
        try:
            gps_ifd = exif.get_ifd(0x8825)
        except Exception:
            gps_ifd = {}

        if gps_ifd:
            try:
                lat = gps_ifd.get(2)
                lat_ref = gps_ifd.get(1)
                lon = gps_ifd.get(4)
                lon_ref = gps_ifd.get(3)
                if lat and lon and lat_ref and lon_ref:
                    lat_val = float(lat[0]) + float(lat[1]) / 60 + float(lat[2]) / 3600
                    lon_val = float(lon[0]) + float(lon[1]) / 60 + float(lon[2]) / 3600
                    if lat_ref == 'S':
                        lat_val = -lat_val
                    if lon_ref == 'W':
                        lon_val = -lon_val
                    meta['gps'] = {'lat': round(lat_val, 6), 'lon': round(lon_val, 6)}
            except Exception:
                pass
    except Exception:
        pass

    return meta


def process_file(filepath, category):
    filename = os.path.basename(filepath)
    name, ext = os.path.splitext(filename)
    ext = ext.lower()

    if ext not in IMAGE_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
        logger.warning(f"Unsupported file type: {filename}")
        return

    if ext in VIDEO_EXTENSIONS:
        thumb_source_path = filepath
        if ext == '.m4v':
            source_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
            mp4_filename = f"{name}.mp4"
            mp4_filepath = os.path.join(source_dir, mp4_filename)
            try:
                logger.info(f"Remuxing {filename} to {mp4_filename}")
                (
                    ffmpeg
                    .input(filepath)
                    .output(mp4_filepath, codec='copy')
                    .overwrite_output()
                    .run()
                )
                os.remove(filepath)
                logger.info(f"Successfully remuxed {filename} to {mp4_filename}")
                thumb_source_path = mp4_filepath
            except ffmpeg.Error as e:
                logger.error(f"Error converting {filename}: {e.stderr.decode()}")
                return

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
        return

    # --- Image processing ---
    try:
        exif_bytes = b''

        if ext in ('.heic', '.heif'):
            logger.info(f"Processing HEIC/HEIF image: {filename}")
            heif_file = pyheif.read(filepath)
            image = Image.frombytes(
                heif_file.mode,
                heif_file.size,
                heif_file.data,
                "raw",
                heif_file.mode,
                heif_file.stride,
            )
            original_format = 'JPEG'
            save_extension = '.jpeg'

            # Extract EXIF from HEIC metadata
            try:
                for meta_item in (heif_file.metadata or []):
                    if meta_item.get('type') == 'Exif':
                        raw = meta_item['data']
                        if raw[:4] == b'Exif':
                            exif_bytes = raw
                        elif raw[:2] in (b'MM', b'II'):
                            exif_bytes = b'Exif\x00\x00' + raw
                        else:
                            exif_bytes = raw
                        break
            except Exception:
                pass
            if exif_bytes:
                image.info['exif'] = exif_bytes
        else:
            logger.info(f"Processing image: {filename}")
            image = Image.open(filepath)
            original_format = image.format
            save_extension = ext

        # Normalize orientation based on EXIF.
        # Pillow's exif_transpose also updates image.info['exif'] with orientation removed.
        try:
            image = ImageOps.exif_transpose(image)
        except Exception:
            pass

        # Get EXIF bytes after transpose (orientation tag already fixed by Pillow)
        exif_bytes = image.info.get('exif', b'')

        # Extract metadata for display before any mode conversion
        photo_meta = extract_photo_metadata(image)

        # Handle transparency
        if image.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
            original_format = 'JPEG'
            save_extension = '.jpeg'
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        sizes = {
            'largest': (2880, 1620),
            'medium': (1920, 1080),
            'thumbnail': (400, 400),
        }

        saved_dimensions = {}
        for size_name, size in sizes.items():
            img_copy = image.copy()
            save_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, size_name)
            os.makedirs(save_dir, exist_ok=True)

            if size_name == 'largest':
                if image.width > size[0] or image.height > size[1]:
                    img_copy.thumbnail(size)
                save_path = os.path.join(save_dir, f'{name}{save_extension}')
                save_kwargs = {'optimize': True, 'progressive': True}
                if exif_bytes:
                    save_kwargs['exif'] = exif_bytes
                if (original_format or '').upper() == 'JPEG' or save_path.lower().endswith(('.jpg', '.jpeg')):
                    img_copy.save(save_path, 'JPEG', quality=100, **save_kwargs)
                else:
                    img_copy.save(save_path, original_format)
                logger.info(f"Saved largest image: {save_path}")

            elif size_name == 'medium':
                img_copy.thumbnail(size)
                save_path = os.path.join(save_dir, f'{name}.jpeg')
                save_kwargs = {'optimize': True, 'progressive': True}
                if exif_bytes:
                    save_kwargs['exif'] = exif_bytes
                img_copy.save(save_path, 'JPEG', quality=IMAGE_QUALITY, **save_kwargs)
                logger.info(f"Saved medium image: {save_path}")

            elif size_name == 'thumbnail':
                img_copy.thumbnail(size)
                save_path = os.path.join(save_dir, f'{name}.jpeg')
                # Thumbnails: no EXIF to keep file size small
                img_copy.save(save_path, 'JPEG', quality=THUMBNAIL_QUALITY, optimize=True, progressive=True)
                logger.info(f"Saved thumbnail image: {save_path}")

            saved_dimensions[size_name] = {'width': img_copy.width, 'height': img_copy.height}

        # Persist dimensions and metadata
        try:
            dimensions_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'dimensions.json')
            os.makedirs(os.path.dirname(dimensions_path), exist_ok=True)
            if os.path.exists(dimensions_path):
                with open(dimensions_path, 'r') as f:
                    dims_data = json.load(f)
            else:
                dims_data = {}
            saved_dimensions['meta'] = photo_meta
            dims_data[name] = saved_dimensions
            with open(dimensions_path, 'w') as f:
                json.dump(dims_data, f)
            logger.info(f"Updated dimensions + metadata: {dimensions_path}")
        except Exception as e:
            logger.warning(f"Could not write dimensions metadata for {filename}: {e}")

    except Exception as e:
        logger.error(f"Error processing image {filename}: {str(e)}")

def build_tree_data(categories):
    category_set = set(categories)
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
                'text': name,
                'href': url_for('category_view', category=full_path),
                'selectable': True,
                'isRealCategory': full_path in category_set
            }
            if subtree:
                node['nodes'] = build_nodes(subtree, full_path)
            nodes.append(node)
        return nodes
    return build_nodes(tree)

def build_parent_options(categories):
    """Build a flat indented list of parent options from hierarchical categories."""
    tree = {}
    for category in categories:
        parts = category.split('-')
        current_level = tree
        for part in parts:
            if part not in current_level:
                current_level[part] = {}
            current_level = current_level[part]
    options = []
    def walk(current_dict, parent_path='', depth=0):
        for name in sorted(current_dict.keys()):
            full_path = f"{parent_path}-{name}" if parent_path else name
            options.append({'path': full_path, 'label': ('— ' * depth) + name})
            walk(current_dict[name], full_path, depth + 1)
    walk(tree)
    return options

class CategoryForm(FlaskForm):
    category_name = StringField('Category Name', validators=[DataRequired()])
    submit = SubmitField('Create Category')

@app.route('/')
def index():
    categories = _list_categories()
    treeData = build_tree_data(categories)
    return render_template('index.html', categories=categories, treeData=treeData)

@app.route('/admin')
def admin_dashboard():
    categories = _list_categories()
    form = CategoryForm()
    parent_options = build_parent_options(categories)
    return render_template('admin.html', categories=categories, form=form, parent_options=parent_options)

@app.route('/admin/category-counts')
def category_counts_api():
    def generate():
        categories = _list_categories()
        base = app.config['UPLOAD_FOLDER']
        for cat in categories:
            photos = 0
            videos = 0
            largest_dir = os.path.join(base, cat, 'largest')
            if os.path.isdir(largest_dir):
                for f in os.listdir(largest_dir):
                    if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS:
                        photos += 1
            source_dir = os.path.join(base, cat, 'source')
            if os.path.isdir(source_dir):
                for f in os.listdir(source_dir):
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                        videos += 1
            yield f"data: {json.dumps({'cat': cat, 'photos': photos, 'videos': videos})}\n\n"
    return Response(generate(), mimetype='text/event-stream')

@app.route('/admin/duplicates/scan')
def scan_duplicates():
    def generate():
        base = app.config['UPLOAD_FOLDER']
        categories = sorted(
            c for c in os.listdir(base)
            if os.path.isdir(os.path.join(base, c)) and not c.startswith('.')
        )
        total_cats = len(categories)

        yield f"data: {json.dumps({'type': 'log', 'message': f'Starting scan across {total_cats} categories...'})}\n\n"

        file_index = {}
        total_files = 0
        for cat_idx, cat in enumerate(categories, 1):
            source_dir = os.path.join(base, cat, 'source')
            if not os.path.isdir(source_dir):
                yield f"data: {json.dumps({'type': 'log', 'message': f'[{cat_idx}/{total_cats}] {cat}: no source dir, skipping'})}\n\n"
                continue
            file_count = 0
            for fname in os.listdir(source_dir):
                fpath = os.path.join(source_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    continue
                file_count += 1
                total_files += 1
                name = os.path.splitext(fname)[0]
                ext_lower = os.path.splitext(fname)[1].lower()
                thumb_url = None
                if ext_lower in IMAGE_EXTENSIONS:
                    thumb_rel = os.path.join(cat, 'thumbnail', name + '.jpeg')
                    if os.path.exists(os.path.join(base, thumb_rel)):
                        thumb_url = url_for('uploaded_file', filename=thumb_rel)
                elif ext_lower in VIDEO_EXTENSIONS:
                    thumb_rel = os.path.join(cat, 'video_thumbnail', name + '.jpeg')
                    if os.path.exists(os.path.join(base, thumb_rel)):
                        thumb_url = url_for('uploaded_file', filename=thumb_rel)
                file_index.setdefault((fname, size), []).append({
                    'category': cat,
                    'thumbnail': thumb_url,
                })
            yield f"data: {json.dumps({'type': 'log', 'message': f'[{cat_idx}/{total_cats}] {cat}: {file_count} files scanned'})}\n\n"

        groups = []
        for (fname, size), locations in file_index.items():
            if len(locations) >= 2:
                groups.append({'filename': fname, 'size': size, 'locations': locations})

        yield f"data: {json.dumps({'type': 'log', 'message': f'Scan complete. {total_files} files across {total_cats} categories, {len(groups)} duplicate group(s) found.'})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'groups': groups})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/admin/duplicates/delete', methods=['POST'])
def delete_duplicate():
    data = request.get_json(silent=True) or {}
    category = data.get('category')
    filename = data.get('filename')
    if not category or not filename:
        return jsonify({'status': 'fail', 'message': 'Missing category or filename'}), 400
    base = app.config['UPLOAD_FOLDER']
    name = os.path.splitext(filename)[0]
    # Delete source file
    source_path = os.path.join(base, category, 'source', filename)
    if os.path.exists(source_path):
        os.remove(source_path)
    # Delete all derived files by name (handles .jpg vs .jpeg mismatch)
    for sub in ['largest', 'medium', 'thumbnail']:
        sub_dir = os.path.join(base, category, sub)
        if os.path.isdir(sub_dir):
            for f in os.listdir(sub_dir):
                if os.path.splitext(f)[0] == name:
                    try:
                        os.remove(os.path.join(sub_dir, f))
                    except OSError:
                        pass
    # Delete video thumbnail
    vt_path = os.path.join(base, category, 'video_thumbnail', name + '.jpeg')
    if os.path.exists(vt_path):
        try:
            os.remove(vt_path)
        except OSError:
            pass
    # Clean dimensions.json
    dims_path = os.path.join(base, category, 'dimensions.json')
    if os.path.exists(dims_path):
        try:
            with open(dims_path, 'r') as f:
                dims = json.load(f)
            if name in dims:
                del dims[name]
                with open(dims_path, 'w') as f:
                    json.dump(dims, f)
        except Exception:
            pass
    return jsonify({'status': 'success', 'message': f"'{filename}' deleted from '{category}'."})

@app.route('/category/<category>')
def category_view(category):
    largest_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'largest')
    source_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
    dimensions_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'dimensions.json')

    images = []
    videos = []

    if os.path.exists(dimensions_path):
        with open(dimensions_path, 'r') as f:
            all_dimensions = json.load(f)
    else:
        all_dimensions = {}

    if os.path.exists(largest_dir):
        image_files = os.listdir(largest_dir)
        for file in image_files:
            name, ext = os.path.splitext(file)
            ext = ext.lower()
            if ext in IMAGE_EXTENSIONS:
                width = all_dimensions.get(name, {}).get('largest', {}).get('width')
                height = all_dimensions.get(name, {}).get('largest', {}).get('height')
                if width is None or height is None:
                    try:
                        img_path = os.path.join(largest_dir, file)
                        with Image.open(img_path) as im:
                            width, height = im.size
                    except Exception:
                        width, height = 1024, 768

                photo_meta = all_dimensions.get(name, {}).get('meta', {})

                images.append({
                    'name': name,
                    'ext': ext,
                    'filename': file,
                    'width': width,
                    'height': height,
                    'meta': photo_meta,
                })

        # Sort images by EXIF date (newest first); photos without date go to the end
        images.sort(key=lambda img: img['meta'].get('date', ''), reverse=True)

    if os.path.exists(source_dir):
        video_files = os.listdir(source_dir)
        for file in video_files:
            name, ext = os.path.splitext(file)
            ext = ext.lower()
            if ext in VIDEO_EXTENSIONS:
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
                })

    all_categories = _list_categories()
    parent_options = build_parent_options(all_categories)
    return render_template('category.html', category=category, images=images, videos=videos, all_categories=all_categories, parent_options=parent_options)

@app.route('/category/create', methods=['POST'])
def create_category():
    form = CategoryForm()
    if form.validate_on_submit():
        parent = request.form.get('parent_category', '').strip()
        child = secure_filename(form.category_name.data)
        category = f"{parent}-{child}" if parent else child
        category_path = os.path.join(app.config['UPLOAD_FOLDER'], category)
        os.makedirs(category_path, exist_ok=True)
        for sub_dir in ['source', 'largest', 'medium', 'thumbnail']:
            os.makedirs(os.path.join(category_path, sub_dir), exist_ok=True)
    return redirect(url_for('admin_dashboard'))

@app.route('/category/delete/<category>', methods=['POST'])
def delete_category(category):
    category_path = os.path.join(app.config['UPLOAD_FOLDER'], category)
    if os.path.exists(category_path):
        try:
            shutil.rmtree(category_path)
            if request.is_json:
                return jsonify({'status': 'success', 'message': f"Category '{category}' deleted."}), 200
            else:
                return redirect(url_for('index'))
        except Exception as e:
            if request.is_json:
                return jsonify({'status': 'fail', 'message': f"Error deleting category: {str(e)}"}), 500
            else:
                return redirect(url_for('index'))
    else:
        if request.is_json:
            return jsonify({'status': 'fail', 'message': 'Category does not exist.'}), 404
        else:
            return redirect(url_for('index'))

@app.route('/category/rename', methods=['POST'])
def rename_category():
    data = request.get_json(silent=True) or {}
    old_name = data.get('old_name', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_name or not new_name:
        return jsonify({'status': 'fail', 'message': 'Both old and new names are required.'}), 400
    new_name = secure_filename(new_name)
    if not new_name:
        return jsonify({'status': 'fail', 'message': 'Invalid new name.'}), 400
    if old_name == new_name:
        return jsonify({'status': 'fail', 'message': 'New name is the same as the old name.'}), 400
    base = app.config['UPLOAD_FOLDER']
    old_path = os.path.join(base, old_name)
    new_path = os.path.join(base, new_name)
    if not os.path.isdir(old_path):
        return jsonify({'status': 'fail', 'message': 'Category does not exist.'}), 404
    if os.path.exists(new_path):
        return jsonify({'status': 'fail', 'message': f"Category '{new_name}' already exists."}), 409
    try:
        # Collect child categories (dirs starting with old_name-)
        children = sorted(
            c for c in os.listdir(base)
            if os.path.isdir(os.path.join(base, c)) and c.startswith(old_name + '-')
        )
        # Rename the category itself
        os.rename(old_path, new_path)
        # Rename children
        renamed = [{'old': old_name, 'new': new_name}]
        for child in children:
            child_new = new_name + child[len(old_name):]
            child_old_path = os.path.join(base, child)
            child_new_path = os.path.join(base, child_new)
            if os.path.isdir(child_old_path) and not os.path.exists(child_new_path):
                os.rename(child_old_path, child_new_path)
                renamed.append({'old': child, 'new': child_new})
        return jsonify({'status': 'success', 'message': f"Renamed '{old_name}' to '{new_name}'.", 'renamed': renamed})
    except Exception as e:
        return jsonify({'status': 'fail', 'message': f"Error renaming category: {str(e)}"}), 500


@app.route('/category/move', methods=['POST'])
def move_category():
    data = request.get_json(silent=True) or {}
    category = data.get('category', '').strip()
    new_parent = data.get('new_parent', '').strip()
    if not category:
        return jsonify({'status': 'fail', 'message': 'Category name is required.'}), 400
    base = app.config['UPLOAD_FOLDER']
    # Validate source exists
    if not os.path.isdir(os.path.join(base, category)):
        return jsonify({'status': 'fail', 'message': 'Category does not exist.'}), 404
    # Validate new_parent exists (if non-empty)
    if new_parent and not os.path.isdir(os.path.join(base, new_parent)):
        return jsonify({'status': 'fail', 'message': f"Parent category '{new_parent}' does not exist."}), 404
    # Cannot move into itself or its own descendant
    if new_parent == category or new_parent.startswith(category + '-'):
        return jsonify({'status': 'fail', 'message': 'Cannot move a category into itself or its own descendant.'}), 400
    # Extract leaf name and compute new name
    leaf = category.rsplit('-', 1)[-1]
    new_name = new_parent + '-' + leaf if new_parent else leaf
    # No-op check
    if new_name == category:
        return jsonify({'status': 'fail', 'message': 'Category is already in that location.'}), 400
    # Destination must not exist
    if os.path.exists(os.path.join(base, new_name)):
        return jsonify({'status': 'fail', 'message': f"Category '{new_name}' already exists."}), 409
    # Collect children
    children = sorted(
        c for c in os.listdir(base)
        if os.path.isdir(os.path.join(base, c)) and c.startswith(category + '-')
    )
    # Pre-check all child renames for conflicts
    for child in children:
        child_new = new_name + child[len(category):]
        if os.path.exists(os.path.join(base, child_new)):
            return jsonify({'status': 'fail', 'message': f"Cannot move: '{child_new}' already exists."}), 409
    try:
        os.rename(os.path.join(base, category), os.path.join(base, new_name))
        renamed = [{'old': category, 'new': new_name}]
        for child in children:
            child_new = new_name + child[len(category):]
            child_old_path = os.path.join(base, child)
            child_new_path = os.path.join(base, child_new)
            if os.path.isdir(child_old_path) and not os.path.exists(child_new_path):
                os.rename(child_old_path, child_new_path)
                renamed.append({'old': child, 'new': child_new})
        return jsonify({'status': 'success', 'message': f"Moved '{category}' to '{new_name}'.", 'renamed': renamed})
    except Exception as e:
        return jsonify({'status': 'fail', 'message': f"Error moving category: {str(e)}"}), 500


@app.route('/upload/<category>', methods=['GET', 'POST'])
def upload_file(category):
    if request.method == 'POST':
        if 'photos[]' not in request.files:
            return jsonify({'status': 'fail', 'message': 'No file part'}), 400
        files = request.files.getlist('photos[]')
        if not files or files[0].filename == '':
            return jsonify({'status': 'fail', 'message': 'No selected files'}), 400
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
                os.makedirs(dest_dir, exist_ok=True)
                filepath = os.path.join(dest_dir, filename)
                file.save(filepath)
                process_file(filepath, category)
        return jsonify({'status': 'success', 'message': 'Files uploaded successfully.'}), 200
    form = CategoryForm()
    return render_template('upload.html', category=category, form=form)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, conditional=True)

@app.route('/download_category/<category>')
def download_category(category):
    size = request.args.get('size', 'largest')
    valid_sizes = ['source', 'largest', 'medium']
    if size not in valid_sizes:
        return jsonify({'status': 'fail', 'message': 'Invalid size parameter.'}), 400

    images_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, size)
    if not os.path.exists(images_dir):
        return jsonify({'status': 'fail', 'message': 'Size not found.'}), 404

    image_filenames = [f for f in os.listdir(images_dir) if os.path.splitext(f)[1].lower() not in VIDEO_EXTENSIONS]
    image_paths = [os.path.join(images_dir, filename) for filename in image_filenames]

    if not image_filenames:
        return jsonify({'status': 'fail', 'message': 'No files to download.'}), 404

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, filename in zip(image_paths, image_filenames):
            zip_file.write(file_path, arcname=filename)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name=f'{category}_{size}_files.zip')

@app.route('/download_videos/<category>')
def download_videos(category):
    source_dir = os.path.join(app.config['UPLOAD_FOLDER'], category, 'source')
    if not os.path.exists(source_dir):
        return jsonify({'status': 'fail', 'message': 'Category not found.'}), 404

    video_filenames = [f for f in os.listdir(source_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS]
    video_paths = [os.path.join(source_dir, f) for f in video_filenames]

    if not video_filenames:
        return jsonify({'status': 'fail', 'message': 'No videos to download.'}), 404

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, filename in zip(video_paths, video_filenames):
            zip_file.write(file_path, arcname=filename)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name=f'{category}_videos.zip')

@app.route('/delete_photo/<category>/<filename>', methods=['POST'])
def delete_photo(category, filename):
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

    name, ext = os.path.splitext(filename)
    if ext.lower() in VIDEO_EXTENSIONS:
        poster_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'video_thumbnail', f'{name}.jpeg')
        if os.path.exists(poster_path):
            try:
                os.remove(poster_path)
            except Exception as e:
                success = False
                messages.append(f'Error deleting video thumbnail: {str(e)}')

    # Also clean up dimensions.json entry
    try:
        dimensions_path = os.path.join(app.config['UPLOAD_FOLDER'], category, 'dimensions.json')
        if os.path.exists(dimensions_path):
            with open(dimensions_path, 'r') as f:
                dims_data = json.load(f)
            if name in dims_data:
                del dims_data[name]
                with open(dimensions_path, 'w') as f:
                    json.dump(dims_data, f)
    except Exception:
        pass

    if success:
        return jsonify({'status': 'success', 'message': f"'{filename}' deleted."}), 200
    else:
        return jsonify({'status': 'fail', 'message': ' '.join(messages)}), 500

@app.route('/photos/delete', methods=['POST'])
def delete_photos_bulk():
    data = request.get_json(silent=True) or {}
    category = data.get('category', '').strip()
    filenames = data.get('filenames', [])
    if not category or not filenames:
        return jsonify({'status': 'fail', 'message': 'Missing required fields.'}), 400
    base = app.config['UPLOAD_FOLDER']
    if not os.path.isdir(os.path.join(base, category)):
        return jsonify({'status': 'fail', 'message': 'Category does not exist.'}), 404
    deleted = []
    errors = []
    names_to_remove = []
    for filename in filenames:
        name, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        try:
            # Delete from source
            src = os.path.join(base, category, 'source', filename)
            if os.path.exists(src):
                os.remove(src)
            # Delete derived files by stem (handles extension mismatches)
            for sub in ['largest', 'medium', 'thumbnail']:
                sub_dir = os.path.join(base, category, sub)
                if os.path.isdir(sub_dir):
                    for f in os.listdir(sub_dir):
                        if os.path.splitext(f)[0] == name:
                            try:
                                os.remove(os.path.join(sub_dir, f))
                            except OSError:
                                pass
            # Delete video thumbnail
            vt = os.path.join(base, category, 'video_thumbnail', name + '.jpeg')
            if os.path.exists(vt):
                try:
                    os.remove(vt)
                except OSError:
                    pass
            names_to_remove.append(name)
            deleted.append(filename)
        except Exception as e:
            errors.append(f'{filename}: {str(e)}')
    # Update dimensions.json once for all deleted files
    if names_to_remove:
        dims_path = os.path.join(base, category, 'dimensions.json')
        if os.path.exists(dims_path):
            try:
                with open(dims_path, 'r') as f:
                    dims = json.load(f)
                for n in names_to_remove:
                    dims.pop(n, None)
                with open(dims_path, 'w') as f:
                    json.dump(dims, f)
            except Exception:
                pass
    if errors:
        return jsonify({'status': 'partial', 'message': f'Deleted {len(deleted)}, errors: {"; ".join(errors)}', 'deleted': deleted}), 207
    return jsonify({'status': 'success', 'message': f'{len(deleted)} photo(s) deleted.', 'deleted': deleted})


@app.route('/photos/move', methods=['POST'])
def move_photos():
    data = request.get_json(silent=True) or {}
    source_cat = data.get('source_category', '').strip()
    dest_cat = data.get('dest_category', '').strip()
    filenames = data.get('filenames', [])
    if not source_cat or not dest_cat or not filenames:
        return jsonify({'status': 'fail', 'message': 'Missing required fields.'}), 400
    if source_cat == dest_cat:
        return jsonify({'status': 'fail', 'message': 'Source and destination are the same.'}), 400
    base = app.config['UPLOAD_FOLDER']
    if not os.path.isdir(os.path.join(base, source_cat)):
        return jsonify({'status': 'fail', 'message': 'Source category does not exist.'}), 404
    if not os.path.isdir(os.path.join(base, dest_cat)):
        return jsonify({'status': 'fail', 'message': 'Destination category does not exist.'}), 404
    moved = []
    errors = []
    for filename in filenames:
        name, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        try:
            if ext_lower in IMAGE_EXTENSIONS:
                subdirs = ['source', 'largest', 'medium', 'thumbnail']
            elif ext_lower in VIDEO_EXTENSIONS:
                subdirs = ['source', 'video_thumbnail']
            else:
                subdirs = ['source']
            for sub in subdirs:
                # For thumbnail/medium, the extension is always .jpeg
                if sub in ('thumbnail', 'medium', 'video_thumbnail'):
                    src_file = os.path.join(base, source_cat, sub, f'{name}.jpeg')
                else:
                    src_file = os.path.join(base, source_cat, sub, filename)
                if os.path.exists(src_file):
                    dest_dir = os.path.join(base, dest_cat, sub)
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_file = os.path.join(dest_dir, os.path.basename(src_file))
                    shutil.move(src_file, dest_file)
            # Also try to move the largest file which may have different extension for HEIC
            if ext_lower in IMAGE_EXTENSIONS:
                largest_dir = os.path.join(base, source_cat, 'largest')
                if os.path.isdir(largest_dir):
                    for f in os.listdir(largest_dir):
                        if os.path.splitext(f)[0] == name:
                            src_f = os.path.join(largest_dir, f)
                            dest_dir = os.path.join(base, dest_cat, 'largest')
                            os.makedirs(dest_dir, exist_ok=True)
                            dest_f = os.path.join(dest_dir, f)
                            if not os.path.exists(dest_f):
                                shutil.move(src_f, dest_f)
            # Move dimensions.json entry
            src_dims = os.path.join(base, source_cat, 'dimensions.json')
            dest_dims = os.path.join(base, dest_cat, 'dimensions.json')
            if os.path.exists(src_dims):
                with open(src_dims, 'r') as f:
                    src_data = json.load(f)
                if name in src_data:
                    entry = src_data.pop(name)
                    with open(src_dims, 'w') as f:
                        json.dump(src_data, f)
                    dest_data = {}
                    if os.path.exists(dest_dims):
                        with open(dest_dims, 'r') as f:
                            dest_data = json.load(f)
                    dest_data[name] = entry
                    with open(dest_dims, 'w') as f:
                        json.dump(dest_data, f)
            moved.append(filename)
        except Exception as e:
            errors.append(f'{filename}: {str(e)}')
    if errors:
        return jsonify({'status': 'partial', 'message': f'Moved {len(moved)}, errors: {"; ".join(errors)}', 'moved': moved}), 207
    return jsonify({'status': 'success', 'message': f'{len(moved)} photo(s) moved.', 'moved': moved})


@app.route('/download_single/<category>/<size>/<filename>')
def download_single(category, size, filename):
    name, ext = os.path.splitext(filename)
    if ext.lower() == '.m4v' and size == 'source':
        filename = f"{name}.mp4"
    valid_sizes = ['source', 'largest', 'medium']
    if size not in valid_sizes:
        abort(404)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], category, size, filename)
    if not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=filename)

@app.errorhandler(RequestEntityTooLarge)
def handle_file_size_error(e):
    return jsonify({'status': 'fail', 'message': 'File too large. Maximum upload size is 5GB.'}), 413

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
