"""Smart Classroom Management System - Flask application entry point."""

from __future__ import annotations

import base64
import datetime
from functools import wraps
import io
import logging
import os
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from firebase_admin import firestore
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from openpyxl import Workbook
from PIL import Image
import requests
from werkzeug.utils import secure_filename

from classroom_seed import seed_initial_data
from face_utils import get_face_encoding, match_face_encoding
from storage import LocalAuthError, create_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, 'temp')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
LOCAL_DATA_DIR = os.path.join(BASE_DIR, 'local_data')
for directory in (TEMP_DIR, UPLOAD_DIR, LOCAL_DATA_DIR):
    os.makedirs(directory, exist_ok=True)


def _load_or_create_secret_key() -> str:
    """Return an environment secret key or a stable local secret across reloads."""
    env_key = os.environ.get('FLASK_SECRET_KEY')
    if env_key:
        return env_key
    secret_file = os.path.join(LOCAL_DATA_DIR, '.flask_secret')
    try:
        if os.path.exists(secret_file):
            with open(secret_file, 'r', encoding='utf-8') as f:
                existing = f.read().strip()
            if existing:
                return existing
        generated = secrets.token_hex(32)
        with open(secret_file, 'w', encoding='utf-8') as f:
            f.write(generated)
        return generated
    except OSError:
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=14),
)

# Firebase Web API Key for REST authentication calls
API_KEY = os.environ.get('FIREBASE_WEB_API_KEY', 'AIzaSyAkPeCdgxk1TyM8KKFjZc-ebbEt1KK41tU')
REQUEST_TIMEOUT_SECONDS = 15

# Initialize backend (live Firebase or automatic local JSON fallback)
STORAGE_MODE = os.environ.get('CLASSROOM_STORAGE', 'auto')
SERVICE_ACCOUNT_PATH = os.environ.get('FIREBASE_SERVICE_ACCOUNT', os.path.join(BASE_DIR, 'serviceAccountKey.json'))
LOCAL_DB_PATH = os.environ.get('CLASSROOM_LOCAL_DB', os.path.join(LOCAL_DATA_DIR, 'classroom_db.json'))

db, auth, BACKEND_MODE, BACKEND_REASON = create_backend(
    service_account_path=SERVICE_ACCOUNT_PATH,
    storage_mode=STORAGE_MODE,
    local_db_path=LOCAL_DB_PATH,
)
logger.info("Smart Classroom backend initialized in '%s' mode: %s", BACKEND_MODE, BACKEND_REASON)

try:
    seed_initial_data(db)
except Exception as exc:
    logger.warning("Initial data seeding skipped: %s", exc)


ALLOWED_SUBMISSION_EXTS = {'.pdf', '.doc', '.docx', '.py', '.zip', '.txt', '.png', '.jpg', '.jpeg'}
ALLOWED_MATERIAL_EXTS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.zip', '.txt', '.png', '.jpg', '.jpeg'}
ALLOWED_VIDEO_EXTS = {'.mp4', '.webm', '.ogg', '.mov'}
ALLOWED_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}

DEFAULT_EXAM_DATA: dict[str, list[dict[str, Any]]] = {
    'Mid-Term Examination': [
        {'subject': 'Mathematics', 'marks': 85},
        {'subject': 'Physics', 'marks': 78},
        {'subject': 'Computer Science', 'marks': 92},
        {'subject': 'English', 'marks': 88},
    ],
    'Final Examination': [
        {'subject': 'Mathematics', 'marks': 90},
        {'subject': 'Physics', 'marks': 82},
        {'subject': 'Computer Science', 'marks': 95},
        {'subject': 'English', 'marks': 85},
    ],
    'Practical Assessment': [
        {'subject': 'Lab Work', 'marks': 88},
        {'subject': 'Project', 'marks': 90},
    ],
    'Quiz 1: Data Structures': [
        {'subject': 'Data Structures', 'marks': 80},
    ],
}

NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z.\-'\s]*$")
VALID_YEARS = {'1st Year', '2nd Year', '3rd Year', '4th Year', 'PG 1st Year', 'PG 2nd Year'}


def _is_api_request() -> bool:
    return request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json'


def login_required(f):
    """Require an authenticated student session."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if _is_api_request():
                return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


def teacher_login_required(f):
    """Require an authenticated teacher session."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'teacher_id' not in session:
            if _is_api_request():
                return jsonify({'status': 'error', 'message': 'Teacher authentication required'}), 401
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


def authenticated_required(f):
    """Require either an authenticated student or teacher session."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session and 'teacher_id' not in session:
            if _is_api_request():
                return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _serialize_timestamp(value: Any) -> str:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value) if value is not None else ''


def _photo_uri_from_doc(doc_data: dict[str, Any], default_url: str) -> str:
    if doc_data.get('profile_photo_b64'):
        return f"data:image/jpeg;base64,{doc_data['profile_photo_b64']}"
    if doc_data.get('face_image'):
        return f"data:image/jpeg;base64,{doc_data['face_image']}"
    return default_url


def _get_student_profile(user_id: str) -> dict[str, Any] | None:
    doc = db.collection('users').document(user_id).get()
    if not doc.exists:
        return None
    user_data = doc.to_dict() or {}
    user_data['uid'] = doc.id
    user_data['name'] = user_data.get('name', 'Unknown Student')
    user_data['regNo'] = user_data.get('regNo', 'Unknown RegNo')
    user_data['email'] = user_data.get('email', 'Not available')
    user_data['branch'] = user_data.get('branch', 'Not available')
    user_data['year'] = user_data.get('year', 'Not available')
    user_data['phone'] = user_data.get('phone', 'Not available')
    user_data['dob'] = user_data.get('dob', 'Not available')
    user_data['gender'] = user_data.get('gender', 'Not available')
    user_data['address'] = user_data.get('address', 'Not available')
    user_data['school'] = user_data.get('school', 'Not available')
    user_data['schoolAddress'] = user_data.get('schoolAddress', 'Not available')
    user_data['schoolId'] = user_data.get('schoolId', 'Not available')
    user_data['classroom'] = user_data.get('classroom', 'Not available')
    user_data['subjects'] = user_data.get('subjects', ['No subjects'])
    user_data['email_notifications'] = user_data.get('email_notifications', True)
    user_data['profile_photo'] = _photo_uri_from_doc(
        user_data,
        'https://picsum.photos/seed/student123/180/180.jpg',
    )
    return user_data


def _get_teacher_profile(teacher_id: str) -> dict[str, Any] | None:
    doc = db.collection('teachers').document(teacher_id).get()
    if not doc.exists:
        return None
    teacher_data = doc.to_dict() or {}
    teacher_data['uid'] = doc.id
    teacher_data['name'] = teacher_data.get('name', 'Unknown Teacher')
    teacher_data['employeeId'] = teacher_data.get('employeeId', 'Unknown Employee ID')
    teacher_data['email'] = teacher_data.get('email', 'Not available')
    teacher_data['phone'] = teacher_data.get('phone', 'Not available')
    teacher_data['subject'] = teacher_data.get('subject', 'Not available')
    teacher_data['department'] = teacher_data.get('department', 'Not available')
    teacher_data['gender'] = teacher_data.get('gender', 'Not available')
    teacher_data['dob'] = teacher_data.get('dob', 'Not available')
    teacher_data['qualification'] = teacher_data.get('qualification', 'Faculty Member')
    teacher_data['office_location'] = teacher_data.get('office_location', 'Academic Block A')
    teacher_data['office_hours'] = teacher_data.get('office_hours', 'Mon-Fri, 10:00 AM - 4:00 PM')
    teacher_data['bio'] = teacher_data.get('bio', 'Dedicated educator committed to student success.')
    teacher_data['email_notifications'] = teacher_data.get('email_notifications', True)
    teacher_data['profile_photo'] = _photo_uri_from_doc(
        teacher_data,
        'https://picsum.photos/seed/teacher123/180/180.jpg',
    )
    return teacher_data


def _render_student_page(template_name: str, **context: Any):
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('index'))
    user_data = _get_student_profile(user_id)
    if not user_data:
        return jsonify({'status': 'error', 'message': 'User data not found'}), 404
    return render_template(template_name, user_data=user_data, **context)


def _render_teacher_page(template_name: str, **context: Any):
    teacher_id = session.get('teacher_id')
    if not teacher_id:
        return redirect(url_for('index'))
    teacher_data = _get_teacher_profile(teacher_id)
    if not teacher_data:
        return jsonify({'status': 'error', 'message': 'Teacher data not found'}), 404
    return render_template(template_name, teacher_data=teacher_data, **context)


def _validate_dob(dob_str: str) -> bool:
    try:
        dob = datetime.datetime.strptime(dob_str, '%Y-%m-%d')
    except ValueError:
        return False
    today = datetime.datetime.now()
    min_date = today - datetime.timedelta(days=100 * 365)
    return min_date <= dob <= today


def _validate_common_registration_fields(data: dict[str, Any], required_fields: list[str]) -> tuple[bool, str]:
    for field in required_fields:
        value = str(data.get(field, '')).strip() if field != 'password' else str(data.get(field, ''))
        if not value:
            return False, f'Missing required field: {field}'

    name = str(data['name']).strip()
    if not NAME_PATTERN.match(name):
        return False, 'Name should only contain letters, spaces, dots, hyphens, or apostrophes'

    email = str(data['email']).strip()
    if '@' not in email or '.' not in email.split('@', 1)[-1]:
        return False, 'Invalid email format'

    phone = str(data['phone']).strip()
    if not phone.isdigit() or len(phone) != 10:
        return False, 'Phone number must be 10 digits'

    if not _validate_dob(str(data['dob']).strip()):
        return False, 'Date of birth must be valid and not in the future or over 100 years ago'

    return True, ''


def _save_uploaded_file(file_storage, subdir: str, allowed_exts: set[str]) -> tuple[str, str]:
    """Validate and save an uploaded file inside uploads/<subdir>/ and return (rel_path, original_name)."""
    if not file_storage or not file_storage.filename:
        raise ValueError('No file selected')
    original_name = secure_filename(file_storage.filename)
    if not original_name:
        raise ValueError('Invalid file name')
    _, ext = os.path.splitext(original_name.lower())
    if ext not in allowed_exts:
        raise ValueError(f'Unsupported file type "{ext}". Allowed: {", ".join(sorted(allowed_exts))}')
    target_dir = os.path.join(UPLOAD_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{original_name}"
    full_path = os.path.join(target_dir, stored_name)
    file_storage.save(full_path)
    return f"{subdir}/{stored_name}", original_name


def _image_to_jpeg_base64(raw_bytes: bytes, max_size: tuple[int, int] = (400, 400)) -> str:
    """Validate and normalize an uploaded image into a compact JPEG base64 string."""
    with Image.open(io.BytesIO(raw_bytes)) as img:
        rgb = img.convert('RGB')
        rgb.thumbnail(max_size)
        buf = io.BytesIO()
        rgb.save(buf, format='JPEG', quality=85)
        return base64.b64encode(buf.getvalue()).decode('ascii')


def _extract_youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if 'youtu.be' in host:
        video_id = parsed.path.lstrip('/').split('/')[0]
        return video_id or None
    if 'youtube.com' in host or 'youtube-nocookie.com' in host:
        if parsed.path.startswith('/embed/'):
            return parsed.path.split('/embed/', 1)[1].split('/')[0] or None
        if parsed.path.startswith('/shorts/'):
            return parsed.path.split('/shorts/', 1)[1].split('/')[0] or None
        qs = parse_qs(parsed.query)
        if 'v' in qs and qs['v']:
            return qs['v'][0]
    return None


def _normalize_video_metadata(video_url: str) -> tuple[str, str, str]:
    """Return ``(normalized_url, video_type, thumbnail_url)`` for YouTube, Vimeo, or direct videos."""
    clean_url = (video_url or '').strip()
    yt_id = _extract_youtube_id(clean_url)
    if yt_id:
        return (
            f"https://www.youtube.com/embed/{yt_id}",
            'embed',
            f"https://img.youtube.com/vi/{yt_id}/mqdefault.jpg",
        )
    if 'player.vimeo.com/' in clean_url:
        return clean_url, 'embed', ''
    return clean_url, 'video', ''


def _list_collection_dicts(collection_name: str) -> list[dict[str, Any]]:
    docs = []
    for snap in db.collection(collection_name).stream():
        item = snap.to_dict() or {}
        item.setdefault('id', snap.id)
        docs.append(item)
    return docs


def _authenticate_with_email_password(email: str, password: str) -> tuple[bool, str | None]:
    if BACKEND_MODE == 'local':
        try:
            auth.sign_in_with_password(email, password)
            return True, None
        except LocalAuthError as err:
            return False, err.message

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
    payload = {'email': email, 'password': password, 'returnSecureToken': True}
    response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code == 200:
        return True, None
    error_info = response.json().get('error', {})
    raw_message = error_info.get('message', 'Incorrect password')
    if 'INVALID_PASSWORD' in raw_message:
        return False, 'Incorrect password'
    if 'USER_NOT_FOUND' in raw_message:
        return False, 'User not found'
    if 'INVALID_LOGIN_CREDENTIALS' in raw_message:
        return False, 'Invalid login credentials'
    return False, raw_message


def _compute_analytics_data() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute deterministic student analytics and class aggregates from stored records."""
    users = [snap.to_dict() or {} for snap in db.collection('users').stream()]
    attendance_docs = list(db.collection('attendance').stream())
    submissions = [snap.to_dict() or {} for snap in db.collection('assignment_submissions').stream()]
    academic_docs = {snap.id: (snap.to_dict() or {}) for snap in db.collection('academic_records').stream()}

    total_days = len(attendance_docs)
    present_counts: dict[str, int] = {}
    for att_snap in attendance_docs:
        day_map = att_snap.to_dict() or {}
        for reg_no, entry in day_map.items():
            if isinstance(entry, dict) and entry.get('status') == 'Present':
                present_counts[reg_no] = present_counts.get(reg_no, 0) + 1

    sub_counts: dict[str, int] = {}
    for sub in submissions:
        reg_no = sub.get('regNo')
        if reg_no:
            sub_counts[reg_no] = sub_counts.get(reg_no, 0) + 1

    students_summary: list[dict[str, Any]] = []
    subject_totals: dict[str, list[float]] = {
        'Mathematics': [],
        'Physics': [],
        'Computer Science': [],
        'English': [],
    }
    grade_counts = {'A': 0, 'B': 0, 'C': 0}

    for user_data in users:
        reg_no = str(user_data.get('regNo', '')).strip() or 'N/A'
        name = user_data.get('name', 'Unknown')
        branch = user_data.get('branch', 'General')
        year = user_data.get('year', '1st Year')
        classroom = user_data.get('classroom') or (f"{branch} ({year})" if branch else 'Class 10A')

        if total_days > 0:
            att_pct = round((present_counts.get(reg_no, 0) / total_days) * 100)
        else:
            att_pct = 100 if present_counts.get(reg_no, 0) > 0 else 0

        stored_record = academic_docs.get(reg_no) or {}
        subject_scores = stored_record.get('subjects')
        if not isinstance(subject_scores, dict) or not subject_scores:
            # Deterministic baseline derived from default exam records + submission bonus
            bonus = min(sub_counts.get(reg_no, 0) * 3, 12)
            subject_scores = {
                'Mathematics': min(100, 82 + bonus),
                'Physics': min(100, 76 + bonus),
                'Computer Science': min(100, 88 + bonus),
                'English': min(100, 84 + bonus),
            }

        for subj, score in subject_scores.items():
            subject_totals.setdefault(subj, []).append(float(score))

        avg_score = round(sum(float(v) for v in subject_scores.values()) / max(len(subject_scores), 1))
        if avg_score >= 90:
            performance = 'Excellent'
            grade_counts['A'] += 1
        elif avg_score >= 75:
            performance = 'Good'
            grade_counts['A'] += 1
        elif avg_score >= 60:
            performance = 'Average'
            grade_counts['B'] += 1
        else:
            performance = 'Poor'
            grade_counts['C'] += 1

        students_summary.append({
            'regNo': reg_no,
            'name': name,
            'email': user_data.get('email', 'Not available'),
            'branch': branch,
            'year': year,
            'class': classroom,
            'attendance_pct': att_pct,
            'attendance': f"{att_pct}%",
            'avg_score_num': avg_score,
            'avg_score': f"{avg_score}%",
            'performance': performance,
            'submissions_count': sub_counts.get(reg_no, 0),
            'subjects': subject_scores,
        })

    students_summary.sort(key=lambda item: item['regNo'])
    total_students = len(students_summary)
    if total_students > 0:
        overall_att = round(sum(s['attendance_pct'] for s in students_summary) / total_students)
        overall_score = round(sum(s['avg_score_num'] for s in students_summary) / total_students)
        passing_rate = round((sum(1 for s in students_summary if s['avg_score_num'] >= 60) / total_students) * 100)
        a_pct = round((grade_counts['A'] / total_students) * 100)
        b_pct = round((grade_counts['B'] / total_students) * 100)
        c_pct = max(0, 100 - a_pct - b_pct)
    else:
        overall_att = 0
        overall_score = 0
        passing_rate = 0
        a_pct, b_pct, c_pct = 0, 0, 0

    subject_averages = []
    for subj, scores in subject_totals.items():
        avg_val = round(sum(scores) / len(scores)) if scores else 0
        subject_averages.append({'subject': subj, 'average': avg_val})

    analytics = {
        'total_students': total_students,
        'total_attendance_days': total_days,
        'avg_attendance': overall_att,
        'avg_score': overall_score,
        'passing_rate': passing_rate,
        'subject_averages': subject_averages,
        'grade_distribution': {
            'a_pct': a_pct,
            'b_pct': b_pct,
            'c_pct': c_pct,
            'ab_stop': a_pct + b_pct,
        },
        'classes': sorted({s['class'] for s in students_summary if s['class']}),
        'subjects': [item['subject'] for item in subject_averages],
    }
    return students_summary, analytics


# ==============================================================================
# Health & Public Pages
# ==============================================================================

@app.route('/health')
def health_check():
    return jsonify({'status': 'ok', 'backend_mode': BACKEND_MODE}), 200


@app.route('/')
def index():
    return render_template('intro.html')


@app.route('/entry')
def entry_page():
    return render_template('entry_page.html')


@app.route('/admin_login')
def admin_login_redirect():
    return redirect(url_for('entry_page', notice='admin_unavailable'))


@app.route('/login')
def login():
    return render_template('login.html')


@app.route('/teacher_login')
def teacher_login():
    return render_template('teacher_login.html')


@app.route('/register')
def register_page():
    return render_template('register.html')


@app.route('/teacher_register')
def teacher_register_page():
    return render_template('teacher_register.html')


@app.route('/forgot')
def forgot_page():
    return render_template('forgot.html', backend_mode=BACKEND_MODE)


# ==============================================================================
# Student Pages (+ legacy route aliases)
# ==============================================================================

@app.route('/home')
@login_required
def home():
    notices = sorted(
        _list_collection_dicts('notices'),
        key=lambda n: n.get('created_at', ''),
        reverse=True,
    )[:4]
    return _render_student_page('home.html', notices=notices)


@app.route('/home.html')
def home_alias():
    return redirect(url_for('home'))


@app.route('/attendance.html')
@login_required
def attendance():
    return render_template('attendance.html')


@app.route('/assignments.html')
@login_required
def assignments():
    return _render_student_page('assignments.html')


@app.route('/assignment.html')
def assignment_alias():
    return redirect(url_for('assignments'))


@app.route('/recorded_class.html')
@login_required
def recorded_classes():
    return render_template('recorded_class.html')


@app.route('/notes.html')
@login_required
def notes():
    return render_template('notes.html')


@app.route('/notices.html')
@login_required
def notices():
    return render_template('notices.html')


@app.route('/profile.html')
@login_required
def profile():
    return _render_student_page('profile.html')


@app.route('/settings.html')
@login_required
def settings():
    return _render_student_page('settings.html')


@app.route('/academic_profile.html')
@login_required
def academic_profile():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('index'))
    user_data = _get_student_profile(user_id)
    if not user_data:
        return jsonify({'status': 'error', 'message': 'User data not found'}), 404
    academic_data = {'name': user_data['name'], 'regNo': user_data['regNo']}
    return render_template('academic_profile.html', user_data=academic_data)


# ==============================================================================
# Teacher Pages (+ legacy route aliases)
# ==============================================================================

@app.route('/teacher_home.html')
@teacher_login_required
def teacher_home():
    notices_list = sorted(
        _list_collection_dicts('notices'),
        key=lambda n: n.get('created_at', ''),
        reverse=True,
    )[:4]
    return _render_teacher_page('teacher_home.html', notices=notices_list)


@app.route('/teacher_settings.html')
@teacher_login_required
def teacher_settings():
    return _render_teacher_page('teacher_settings.html')


@app.route('/teacher_fa.html')
@teacher_login_required
def teacher_fa():
    students = []
    for snap in db.collection('users').stream():
        data = snap.to_dict() or {}
        students.append({
            'name': data.get('name', 'Unknown'),
            'regNo': data.get('regNo', 'Not available'),
            'email': data.get('email', 'Not available'),
            'phone': data.get('phone', 'Not available'),
            'gender': data.get('gender', 'Not available'),
            'dob': data.get('dob', 'Not available'),
            'branch': data.get('branch', 'Not available'),
        })
    students.sort(key=lambda x: x['regNo'])
    return _render_teacher_page('teacher_fa.html', students=students)


@app.route('/fa.html')
def fa_alias():
    return redirect(url_for('teacher_fa'))


@app.route('/teacher_assignment.html')
@teacher_login_required
def teacher_assignment():
    return _render_teacher_page('teacher_assignment.html')


@app.route('/teacher_notice.html')
@teacher_login_required
def teacher_notice():
    return _render_teacher_page('teacher_notice.html')


@app.route('/teacher_recorded_class.html')
@teacher_login_required
def teacher_recorded_class():
    return _render_teacher_page('teacher_recorded_class.html')


@app.route('/teacher_query.html')
@teacher_login_required
def teacher_query():
    return _render_teacher_page('teacher_query.html')


@app.route('/teacher_study.html')
@teacher_login_required
def teacher_study():
    return _render_teacher_page('teacher_study.html')


@app.route('/teacher_analytics.html')
@teacher_login_required
def teacher_analytics():
    students_summary, analytics = _compute_analytics_data()
    return _render_teacher_page('teacher_analytics.html', students=students_summary, analytics=analytics)


@app.route('/teacher_attendance.html')
@teacher_login_required
def teacher_attendance():
    return _render_teacher_page('teacher_attendance.html')


@app.route('/teacher_profile.html')
@teacher_login_required
def teacher_profile():
    return _render_teacher_page('teacher_profile.html')


@app.route('/teacher_notes.html')
@teacher_login_required
def teacher_notes():
    return _render_teacher_page('teacher_notes.html')


# ==============================================================================
# File Upload Serving & Report Card Excel Generation
# ==============================================================================

@app.route('/uploads/<path:subpath>')
@authenticated_required
def serve_uploaded_file(subpath: str):
    return send_from_directory(UPLOAD_DIR, subpath, as_attachment=False)


@app.route('/api/generate_report_card', methods=['POST'])
@login_required
def generate_report_card():
    try:
        user_id = session.get('user_id')
        user_data = _get_student_profile(user_id) if user_id else None
        if not user_data:
            return jsonify({'status': 'error', 'message': 'User data not found'}), 404

        data = _json_body()
        exam_name = str(data.get('exam_name', '')).strip()
        if not exam_name:
            return jsonify({'status': 'error', 'message': 'Exam name is required'}), 400
        if exam_name not in DEFAULT_EXAM_DATA:
            return jsonify({'status': 'error', 'message': 'Invalid exam name'}), 400

        marks = DEFAULT_EXAM_DATA[exam_name]
        wb = Workbook()
        ws = wb.active
        ws.title = 'Report Card'

        ws['A1'] = 'Report Card'
        ws['A2'] = 'Student Name'
        ws['B2'] = user_data['name']
        ws['A3'] = 'Registration Number'
        ws['B3'] = user_data['regNo']
        ws['A4'] = 'Examination'
        ws['B4'] = exam_name
        ws['A5'] = 'Date'
        ws['B5'] = datetime.date.today().strftime('%B %d, %Y')
        ws['A7'] = 'Subject'
        ws['B7'] = 'Marks (Out of 100)'

        for idx, subject in enumerate(marks, start=8):
            ws[f'A{idx}'] = subject['subject']
            ws[f'B{idx}'] = subject['marks']

        for col_letter in ('A', 'B'):
            max_len = max((len(str(cell.value or '')) for cell in ws[col_letter]), default=10)
            ws.column_dimensions[col_letter].width = max_len + 2

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{exam_name} Report Card.xlsx",
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    except Exception as exc:
        logger.error("Error generating report card: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


# ==============================================================================
# Authentication & Account Management APIs
# ==============================================================================

@app.route('/api/register', methods=['POST'])
def register_user():
    try:
        data = _json_body()
        required = ['name', 'gender', 'dob', 'branch', 'year', 'email', 'regNo', 'phone', 'password']
        valid, err_msg = _validate_common_registration_fields(data, required)
        if not valid:
            return jsonify({'status': 'error', 'message': err_msg}), 400

        branch = str(data['branch']).strip()
        if not NAME_PATTERN.match(branch):
            return jsonify({'status': 'error', 'message': 'Branch should only contain letters and spaces'}), 400

        year = str(data['year']).strip()
        if year not in VALID_YEARS:
            return jsonify({'status': 'error', 'message': 'Invalid year selected'}), 400

        reg_no = str(data['regNo']).strip()
        email = str(data['email']).strip()

        users_ref = db.collection('users')
        if any(users_ref.where('regNo', '==', reg_no).limit(1).stream()):
            return jsonify({'status': 'error', 'message': 'Registration number already exists'}), 409

        user = auth.create_user(email=email, password=data['password'])
        uid = user.uid

        user_data = {
            'name': str(data['name']).strip(),
            'gender': str(data['gender']).strip(),
            'dob': str(data['dob']).strip(),
            'branch': branch,
            'year': year,
            'email': email,
            'regNo': reg_no,
            'phone': str(data['phone']).strip(),
            'role': 'student',
            'email_notifications': True,
            'created_at': datetime.datetime.now(datetime.timezone.utc),
            'updated_at': datetime.datetime.now(datetime.timezone.utc),
        }
        users_ref.document(uid).set(user_data)
        return jsonify({
            'status': 'success',
            'message': 'Registration successful! Redirecting to login...',
        }), 200
    except Exception as exc:
        logger.error("Error during student registration: %s", exc)
        msg = str(exc)
        if 'EMAIL_EXISTS' in msg:
            return jsonify({'status': 'error', 'message': 'Email already registered'}), 409
        if 'WEAK_PASSWORD' in msg:
            return jsonify({'status': 'error', 'message': 'Password must be at least 6 characters'}), 400
        return jsonify({'status': 'error', 'message': f'An error occurred: {msg}'}), 500


@app.route('/api/teacher_register', methods=['POST'])
def register_teacher():
    try:
        data = _json_body()
        required = ['name', 'gender', 'dob', 'email', 'employeeId', 'department', 'subject', 'phone', 'password']
        valid, err_msg = _validate_common_registration_fields(data, required)
        if not valid:
            return jsonify({'status': 'error', 'message': err_msg}), 400

        employee_id = str(data['employeeId']).strip()
        email = str(data['email']).strip()

        teachers_ref = db.collection('teachers')
        if any(teachers_ref.where('employeeId', '==', employee_id).limit(1).stream()):
            return jsonify({'status': 'error', 'message': 'Employee ID already exists'}), 409

        user = auth.create_user(email=email, password=data['password'])
        uid = user.uid
        auth.set_custom_user_claims(uid, {'role': 'teacher'})

        teacher_data = {
            'name': str(data['name']).strip(),
            'gender': str(data['gender']).strip(),
            'dob': str(data['dob']).strip(),
            'email': email,
            'employeeId': employee_id,
            'department': str(data['department']).strip(),
            'subject': str(data['subject']).strip(),
            'phone': str(data['phone']).strip(),
            'role': 'teacher',
            'email_notifications': True,
            'created_at': datetime.datetime.now(datetime.timezone.utc),
            'updated_at': datetime.datetime.now(datetime.timezone.utc),
        }
        teachers_ref.document(uid).set(teacher_data)
        return jsonify({
            'status': 'success',
            'message': 'Teacher registration successful! Redirecting to login...',
        }), 200
    except Exception as exc:
        logger.error("Error during teacher registration: %s", exc)
        msg = str(exc)
        if 'EMAIL_EXISTS' in msg:
            return jsonify({'status': 'error', 'message': 'Email already registered'}), 409
        if 'WEAK_PASSWORD' in msg:
            return jsonify({'status': 'error', 'message': 'Password must be at least 6 characters'}), 400
        return jsonify({'status': 'error', 'message': f'An error occurred: {msg}'}), 500


@app.route('/api/login', methods=['POST'])
def login_user():
    try:
        data = _json_body()
        reg_no = str(data.get('regNo', '')).strip()
        password = str(data.get('password', ''))
        if not reg_no or not password:
            return jsonify({
                'status': 'error',
                'message': 'Registration number and password are required',
            }), 400

        users_ref = db.collection('users')
        user_doc = next(users_ref.where('regNo', '==', reg_no).limit(1).stream(), None)
        if not user_doc:
            return jsonify({'status': 'error', 'message': 'Registration number not found'}), 404

        user_data = user_doc.to_dict() or {}
        email = user_data['email']
        uid = user_doc.id

        try:
            user = auth.get_user(uid)
            claims = getattr(user, 'custom_claims', None) or {}
            if claims.get('role') == 'teacher':
                return jsonify({'status': 'error', 'message': 'Please use teacher login portal'}), 403
        except auth.UserNotFoundError:
            users_ref.document(uid).delete()
            return jsonify({
                'status': 'error',
                'message': 'User account not found in authentication system. Please register again.',
            }), 404

        ok, auth_err = _authenticate_with_email_password(email, password)
        if not ok:
            return jsonify({'status': 'error', 'message': auth_err}), 401

        session.pop('teacher_id', None)
        session['user_id'] = uid
        session.permanent = bool(data.get('remember'))
        return jsonify({
            'status': 'success',
            'message': 'Login successful! Redirecting to student portal...',
        }), 200
    except Exception as exc:
        logger.error("Error during student login: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


@app.route('/api/teacher_login', methods=['POST'])
def teacher_login_user():
    try:
        data = _json_body()
        login_id = str(data.get('loginId', '')).strip()
        password = str(data.get('password', ''))
        if not login_id or not password:
            return jsonify({
                'status': 'error',
                'message': 'Email or Employee ID and password are required',
            }), 400

        teachers_ref = db.collection('teachers')
        teacher_doc = next(teachers_ref.where('email', '==', login_id).limit(1).stream(), None)
        if not teacher_doc:
            teacher_doc = next(teachers_ref.where('employeeId', '==', login_id).limit(1).stream(), None)
        if not teacher_doc:
            return jsonify({'status': 'error', 'message': 'Email or Employee ID not found'}), 404

        teacher_data = teacher_doc.to_dict() or {}
        email = teacher_data['email']
        tid = teacher_doc.id

        try:
            user = auth.get_user(tid)
            claims = getattr(user, 'custom_claims', None) or {}
            if claims.get('role') != 'teacher':
                return jsonify({'status': 'error', 'message': 'Access denied: Not a teacher account'}), 403
        except auth.UserNotFoundError:
            return jsonify({'status': 'error', 'message': 'User account not found in authentication system'}), 404

        ok, auth_err = _authenticate_with_email_password(email, password)
        if not ok:
            return jsonify({'status': 'error', 'message': auth_err}), 401

        session.pop('user_id', None)
        session['teacher_id'] = tid
        session.permanent = bool(data.get('remember'))
        return jsonify({
            'status': 'success',
            'message': 'Login successful! Redirecting to teacher portal...',
        }), 200
    except Exception as exc:
        logger.error("Error during teacher login: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


@app.route('/api/forgot_password', methods=['POST'])
def forgot_password():
    try:
        data = _json_body()
        email = str(data.get('email', '')).strip()
        if not email or '@' not in email:
            return jsonify({'status': 'error', 'message': 'Please enter a valid email address'}), 400

        if BACKEND_MODE == 'local':
            exists = auth.email_exists(email)
            if not exists:
                return jsonify({'status': 'error', 'message': 'No account found with that email address'}), 404
            return jsonify({
                'status': 'success',
                'message': 'Local mode active: email verified. Use Settings -> Change Password while signed in, or configure live Firebase for email links.',
            }), 200

        url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={API_KEY}"
        response = requests.post(
            url,
            json={'requestType': 'PASSWORD_RESET', 'email': email},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return jsonify({'status': 'success', 'message': 'Reset link sent! Check your inbox (and spam).'}), 200
        err = response.json().get('error', {}).get('message', 'Failed to send password reset email')
        return jsonify({'status': 'error', 'message': err}), 400
    except Exception as exc:
        logger.error("Error during forgot password: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


@app.route('/api/send_password_reset', methods=['POST'])
@authenticated_required
def send_password_reset():
    try:
        if 'teacher_id' in session:
            profile_data = _get_teacher_profile(session['teacher_id'])
        else:
            profile_data = _get_student_profile(session['user_id'])
        if not profile_data:
            return jsonify({'status': 'error', 'message': 'User data not found'}), 404

        email = profile_data.get('email')
        if not email or email == 'Not available':
            return jsonify({'status': 'error', 'message': 'Email not found for user'}), 404

        if BACKEND_MODE == 'local':
            return jsonify({
                'status': 'success',
                'message': f'Password reset instructions prepared for {email} (local mode). Use /forgot or change password directly in Settings.',
            }), 200

        url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={API_KEY}"
        response = requests.post(
            url,
            json={'requestType': 'PASSWORD_RESET', 'email': email},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return jsonify({'status': 'success', 'message': 'Password reset email sent successfully'}), 200
        message = response.json().get('error', {}).get('message', 'Failed to send password reset email')
        return jsonify({'status': 'error', 'message': message}), 400
    except Exception as exc:
        logger.error("Error sending password reset email: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


@app.route('/api/change_password', methods=['POST'])
@authenticated_required
def change_password_direct():
    """Verify current password and update password for signed-in student or teacher."""
    try:
        data = _json_body()
        current_pw = str(data.get('current_password', ''))
        new_pw = str(data.get('new_password', ''))
        if not current_pw or len(new_pw) < 6:
            return jsonify({
                'status': 'error',
                'message': 'Current password and new password (min 6 characters) are required',
            }), 400

        if 'teacher_id' in session:
            profile_data = _get_teacher_profile(session['teacher_id'])
        else:
            profile_data = _get_student_profile(session['user_id'])
        if not profile_data:
            return jsonify({'status': 'error', 'message': 'User not found'}), 404

        ok, auth_err = _authenticate_with_email_password(profile_data['email'], current_pw)
        if not ok:
            return jsonify({'status': 'error', 'message': auth_err or 'Current password is incorrect'}), 401

        if BACKEND_MODE == 'local':
            auth.update_password_by_email(profile_data['email'], new_pw)
        else:
            uid = profile_data['uid']
            auth.update_user(uid, password=new_pw)

        return jsonify({'status': 'success', 'message': 'Password updated successfully'}), 200
    except Exception as exc:
        logger.error("Error updating password: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


@app.route('/api/update_settings', methods=['POST'])
@authenticated_required
def update_settings():
    try:
        data = _json_body()
        email_notifications = bool(data.get('email_notifications', True))
        if 'teacher_id' in session:
            doc_ref = db.collection('teachers').document(session['teacher_id'])
        else:
            doc_ref = db.collection('users').document(session['user_id'])
        if not doc_ref.get().exists:
            return jsonify({'status': 'error', 'message': 'User not found'}), 404
        doc_ref.update({
            'email_notifications': email_notifications,
            'updated_at': datetime.datetime.now(datetime.timezone.utc),
        })
        return jsonify({'status': 'success', 'message': 'Settings updated', 'email_notifications': email_notifications}), 200
    except Exception as exc:
        logger.error("Error updating settings: %s", exc)
        return jsonify({'status': 'error', 'message': 'Failed to update settings'}), 500


@app.route('/api/sign_out', methods=['POST'])
def sign_out():
    session.pop('user_id', None)
    session.pop('teacher_id', None)
    return jsonify({'status': 'success', 'message': 'Signed out successfully'}), 200


@app.route('/api/delete_account', methods=['POST'])
@authenticated_required
def delete_account():
    try:
        if 'teacher_id' in session:
            uid = session['teacher_id']
            db.collection('teachers').document(uid).delete()
            session.pop('teacher_id', None)
        else:
            uid = session['user_id']
            db.collection('users').document(uid).delete()
            session.pop('user_id', None)

        try:
            auth.delete_user(uid)
        except auth.UserNotFoundError:
            pass

        return jsonify({'status': 'success', 'message': 'Account deleted successfully'}), 200
    except Exception as exc:
        logger.error("Error deleting account: %s", exc)
        return jsonify({'status': 'error', 'message': f'An error occurred: {exc}'}), 500


# ==============================================================================
# Profile & Contact APIs
# ==============================================================================

@app.route('/api/student/profile/photo', methods=['POST'])
@login_required
def upload_student_profile_photo():
    try:
        user_id = session['user_id']
        if 'photo' in request.files:
            file_storage = request.files['photo']
            if not file_storage.filename:
                return jsonify({'status': 'error', 'message': 'No image file provided'}), 400
            raw_bytes = file_storage.read()
        else:
            data = _json_body()
            image_b64 = str(data.get('image', ''))
            if not image_b64:
                return jsonify({'status': 'error', 'message': 'No image provided'}), 400
            payload = image_b64.split(',', 1)[1] if ',' in image_b64 else image_b64
            raw_bytes = base64.b64decode(payload)

        encoded_jpeg = _image_to_jpeg_base64(raw_bytes)
        db.collection('users').document(user_id).update({
            'profile_photo_b64': encoded_jpeg,
            'updated_at': datetime.datetime.now(datetime.timezone.utc),
        })
        return jsonify({
            'status': 'success',
            'message': 'Profile photo updated successfully',
            'profile_photo': f"data:image/jpeg;base64,{encoded_jpeg}",
        }), 200
    except Exception as exc:
        logger.error("Error updating student photo: %s", exc)
        return jsonify({'status': 'error', 'message': 'Invalid image file or upload error'}), 400


@app.route('/api/teacher/profile', methods=['GET', 'PUT', 'POST'])
@teacher_login_required
def teacher_profile_api():
    teacher_id = session['teacher_id']
    if request.method == 'GET':
        teacher_data = _get_teacher_profile(teacher_id)
        if not teacher_data:
            return jsonify({'status': 'error', 'message': 'Teacher data not found'}), 404
        return jsonify({'status': 'success', 'profile': teacher_data}), 200

    try:
        updates: dict[str, Any] = {'updated_at': datetime.datetime.now(datetime.timezone.utc)}
        if request.files and 'photo' in request.files and request.files['photo'].filename:
            encoded_jpeg = _image_to_jpeg_base64(request.files['photo'].read())
            updates['profile_photo_b64'] = encoded_jpeg

        source_data = request.form.to_dict() if request.form else _json_body()
        editable_fields = (
            'name', 'phone', 'department', 'subject', 'gender', 'dob',
            'qualification', 'office_location', 'office_hours', 'bio',
        )
        for field in editable_fields:
            if field in source_data and str(source_data[field]).strip():
                val = str(source_data[field]).strip()
                if field == 'name' and not NAME_PATTERN.match(val):
                    return jsonify({'status': 'error', 'message': 'Invalid name format'}), 400
                if field == 'phone' and (not val.isdigit() or len(val) != 10):
                    return jsonify({'status': 'error', 'message': 'Phone number must be 10 digits'}), 400
                if field == 'dob' and not _validate_dob(val):
                    return jsonify({'status': 'error', 'message': 'Invalid date of birth'}), 400
                updates[field] = val

        db.collection('teachers').document(teacher_id).update(updates)
        teacher_data = _get_teacher_profile(teacher_id)
        return jsonify({'status': 'success', 'message': 'Profile updated successfully', 'profile': teacher_data}), 200
    except Exception as exc:
        logger.error("Error updating teacher profile: %s", exc)
        return jsonify({'status': 'error', 'message': f'Failed to update profile: {exc}'}), 400


@app.route('/api/contact', methods=['POST'])
def submit_contact_message():
    data = _json_body()
    name = str(data.get('name', '')).strip()
    email = str(data.get('email', '')).strip()
    message = str(data.get('message', '')).strip()
    if not name or not email or '@' not in email or not message:
        return jsonify({'status': 'error', 'message': 'Please provide name, valid email, and message'}), 400

    msg_id = f"contact-{uuid.uuid4().hex[:12]}"
    db.collection('contact_messages').document(msg_id).set({
        'id': msg_id,
        'name': name,
        'email': email,
        'message': message,
        'created_at': _now_iso(),
    })
    return jsonify({'status': 'success', 'message': 'Thank you! Your message has been received.'}), 200


# ==============================================================================
# Attendance & Face Recognition APIs
# ==============================================================================

@app.route('/api/get_all_students', methods=['GET'])
@authenticated_required
def get_all_students():
    try:
        students = []
        for user in db.collection('users').stream():
            data = user.to_dict() or {}
            students.append({
                'regNo': data.get('regNo', ''),
                'name': data.get('name', ''),
                'branch': data.get('branch', 'Computer Science'),
                'year': data.get('year', ''),
            })
        students.sort(key=lambda x: x['regNo'])
        return jsonify({'status': 'success', 'students': students}), 200
    except Exception as exc:
        logger.error("Error getting all students: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/get_registered_students', methods=['GET'])
@authenticated_required
def get_registered_students():
    try:
        students = []
        for user in db.collection('users').where('face_encoding', '!=', None).stream():
            data = user.to_dict() or {}
            if not data.get('face_encoding'):
                continue
            face_image = data.get('face_image')
            if face_image:
                face_image = f"data:image/jpeg;base64,{face_image}"
            students.append({
                'regNo': data.get('regNo', ''),
                'name': data.get('name', ''),
                'face_image': face_image,
            })
        students.sort(key=lambda x: x['regNo'])
        return jsonify({'status': 'success', 'students': students}), 200
    except Exception as exc:
        logger.error("Error getting registered students: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/register_face', methods=['POST'])
@authenticated_required
def register_face():
    try:
        data = _json_body()
        reg_no = str(data.get('regNo', '')).strip()
        image = data.get('image')
        if not reg_no or not image:
            return jsonify({'status': 'error', 'message': 'Missing regNo or image'}), 400

        encoding = get_face_encoding(image)
        user_doc = next(db.collection('users').where('regNo', '==', reg_no).limit(1).stream(), None)
        if not user_doc:
            return jsonify({'status': 'error', 'message': 'Student not found'}), 404

        image_base64 = image.split(',', 1)[1] if ',' in image else image
        user_doc.reference.update({
            'face_encoding': encoding.tolist(),
            'face_image': image_base64,
            'updated_at': datetime.datetime.now(datetime.timezone.utc),
        })
        return jsonify({'status': 'success', 'message': 'Face registered successfully'}), 200
    except ValueError as ve:
        return jsonify({'status': 'error', 'message': str(ve)}), 400
    except Exception as exc:
        logger.error("Error registering face: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/recognize_face', methods=['POST'])
@authenticated_required
def recognize_face():
    try:
        session_doc = db.collection('attendance_sessions').document('current').get()
        session_state = session_doc.to_dict() if session_doc.exists else {'active': True, 'hour': '1st Hour'}
        if not session_state.get('active', True):
            return jsonify({
                'status': 'error',
                'message': 'Attendance session is currently inactive. Ask your teacher to start a session.',
            }), 403

        data = _json_body()
        image = data.get('image')
        if not image:
            return jsonify({'status': 'error', 'message': 'Missing image'}), 400

        captured_encoding = get_face_encoding(image)
        encodings = []
        students = []
        for user in db.collection('users').where('face_encoding', '!=', None).stream():
            user_data = user.to_dict() or {}
            raw_enc = user_data.get('face_encoding')
            if not raw_enc:
                continue
            encodings.append(raw_enc)
            students.append({
                'uid': user.id,
                'regNo': user_data.get('regNo', ''),
                'name': user_data.get('name', ''),
            })

        if not encodings:
            raise ValueError("No registered faces found. Please register students first.")

        matched_idx, _ = match_face_encoding(encodings, captured_encoding)
        if matched_idx is None:
            raise ValueError("No matching face found")

        matched_student = students[matched_idx]
        today = datetime.date.today().isoformat()
        attendance_ref = db.collection('attendance').document(today)
        attendance_data = attendance_ref.get().to_dict() or {}
        existing = attendance_data.get(matched_student['regNo'])
        if isinstance(existing, dict) and existing.get('status') == 'Present':
            return jsonify({'status': 'error', 'message': 'Attendance already marked for today'}), 400

        attendance_data[matched_student['regNo']] = {
            'status': 'Present',
            'name': matched_student['name'],
            'hour': session_state.get('hour', '1st Hour'),
            'timestamp': _now_iso(),
        }
        attendance_ref.set(attendance_data)
        return jsonify({
            'status': 'success',
            'studentId': matched_student['regNo'],
            'name': matched_student['name'],
        }), 200
    except ValueError as ve:
        return jsonify({'status': 'error', 'message': str(ve)}), 400
    except Exception as exc:
        logger.error("Error recognizing face: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/get_today_attendance', methods=['GET'])
@authenticated_required
def get_today_attendance():
    try:
        today = datetime.date.today().isoformat()
        attendance_ref = db.collection('attendance').document(today)
        raw_data = attendance_ref.get().to_dict() or {}
        formatted = {}
        for reg_no, entry in raw_data.items():
            if isinstance(entry, dict):
                formatted[reg_no] = {
                    'status': entry.get('status', 'Absent'),
                    'name': entry.get('name', ''),
                    'hour': entry.get('hour', '1st Hour'),
                    'timestamp': _serialize_timestamp(entry.get('timestamp')),
                }
        return jsonify({'status': 'success', 'attendance': formatted}), 200
    except Exception as exc:
        logger.error("Error getting today's attendance: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/delete_face', methods=['POST'])
@authenticated_required
def delete_face():
    try:
        data = _json_body()
        reg_no = str(data.get('regNo', '')).strip()
        if not reg_no:
            return jsonify({'status': 'error', 'message': 'Missing regNo'}), 400

        user_doc = next(db.collection('users').where('regNo', '==', reg_no).limit(1).stream(), None)
        if not user_doc:
            return jsonify({'status': 'error', 'message': 'Student not found'}), 404

        user_doc.reference.update({
            'face_encoding': firestore.DELETE_FIELD,
            'face_image': firestore.DELETE_FIELD,
            'updated_at': datetime.datetime.now(datetime.timezone.utc),
        })
        return jsonify({'status': 'success', 'message': 'Face data deleted successfully'}), 200
    except Exception as exc:
        logger.error("Error deleting face data: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/clear_all_faces', methods=['POST'])
@authenticated_required
def clear_all_faces():
    try:
        users = list(db.collection('users').stream())
        if users:
            batch = db.batch()
            for user in users:
                batch.update(user.reference, {
                    'face_encoding': firestore.DELETE_FIELD,
                    'face_image': firestore.DELETE_FIELD,
                    'updated_at': datetime.datetime.now(datetime.timezone.utc),
                })
            batch.commit()
        return jsonify({'status': 'success', 'message': 'All face data cleared'}), 200
    except Exception as exc:
        logger.error("Error clearing all faces: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/reset_today_attendance', methods=['POST'])
@authenticated_required
def reset_today_attendance():
    try:
        today = datetime.date.today().isoformat()
        db.collection('attendance').document(today).delete()
        return jsonify({'status': 'success', 'message': "Today's attendance reset"}), 200
    except Exception as exc:
        logger.error("Error resetting attendance: %s", exc)
        return jsonify({'status': 'error', 'message': 'An error occurred'}), 500


@app.route('/api/attendance/session', methods=['GET', 'POST'])
@authenticated_required
def attendance_session_api():
    session_ref = db.collection('attendance_sessions').document('current')
    if request.method == 'GET':
        doc = session_ref.get()
        state = doc.to_dict() if doc.exists else {'active': True, 'hour': '1st Hour', 'started_at': _now_iso()}
        return jsonify({'status': 'success', 'session': state}), 200

    if 'teacher_id' not in session:
        return jsonify({'status': 'error', 'message': 'Teacher authentication required'}), 403

    data = _json_body()
    action = str(data.get('action', '')).strip().lower()
    active = action == 'start' if action in {'start', 'stop'} else bool(data.get('active', False))
    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    state = {
        'active': active,
        'hour': str(data.get('hour') or '1st Hour'),
        'started_at': _now_iso() if active else '',
        'started_by': teacher_data.get('name', 'Teacher'),
        'updated_at': _now_iso(),
    }
    session_ref.set(state)
    return jsonify({'status': 'success', 'session': state}), 200


@app.route('/api/attendance/records', methods=['GET'])
@authenticated_required
def get_attendance_records():
    try:
        from_date = request.args.get('from_date', '').strip()
        to_date = request.args.get('to_date', '').strip()

        students = []
        student_names = {}
        for snap in db.collection('users').stream():
            u = snap.to_dict() or {}
            reg_no = u.get('regNo', '')
            if reg_no:
                student_names[reg_no] = u.get('name', 'Unknown')
                students.append({'regNo': reg_no, 'name': u.get('name', 'Unknown')})
        students.sort(key=lambda x: x['regNo'])

        att_docs = {snap.id: (snap.to_dict() or {}) for snap in db.collection('attendance').stream()}
        today = datetime.date.today().isoformat()
        if today not in att_docs:
            att_docs[today] = {}

        records = []
        for date_str, day_map in sorted(att_docs.items(), reverse=True):
            if from_date and date_str < from_date:
                continue
            if to_date and date_str > to_date:
                continue

            seen_regs = set()
            for reg_no, entry in day_map.items():
                if not isinstance(entry, dict):
                    continue
                seen_regs.add(reg_no)
                records.append({
                    'date': date_str,
                    'student_id': reg_no,
                    'name': entry.get('name') or student_names.get(reg_no, 'Unknown'),
                    'hour': entry.get('hour', '1st Hour'),
                    'status': entry.get('status', 'Absent'),
                    'timestamp': _serialize_timestamp(entry.get('timestamp')),
                })

            for student in students:
                if student['regNo'] not in seen_regs:
                    records.append({
                        'date': date_str,
                        'student_id': student['regNo'],
                        'name': student['name'],
                        'hour': '1st Hour',
                        'status': 'Absent',
                        'timestamp': '',
                    })

        records.sort(key=lambda r: (r['date'], r['student_id']), reverse=True)
        return jsonify({'status': 'success', 'students': students, 'records': records}), 200
    except Exception as exc:
        logger.error("Error fetching attendance records: %s", exc)
        return jsonify({'status': 'error', 'message': 'Failed to load attendance records'}), 500


@app.route('/api/attendance/mark', methods=['POST'])
@authenticated_required
def mark_attendance_manual():
    try:
        data = _json_body()
        reg_no = str(data.get('regNo') or data.get('student_id') or '').strip()
        date_str = str(data.get('date') or datetime.date.today().isoformat()).strip()
        hour = str(data.get('hour') or '1st Hour').strip()
        status_val = str(data.get('status') or 'Present').strip()
        if status_val not in {'Present', 'Absent'}:
            return jsonify({'status': 'error', 'message': 'Invalid attendance status'}), 400
        if not reg_no:
            return jsonify({'status': 'error', 'message': 'Student registration number is required'}), 400

        student_doc = next(db.collection('users').where('regNo', '==', reg_no).limit(1).stream(), None)
        student_name = (student_doc.to_dict() or {}).get('name', 'Unknown') if student_doc else 'Unknown'

        att_ref = db.collection('attendance').document(date_str)
        day_map = att_ref.get().to_dict() or {}
        day_map[reg_no] = {
            'status': status_val,
            'name': student_name,
            'hour': hour,
            'timestamp': _now_iso(),
        }
        att_ref.set(day_map)
        return jsonify({'status': 'success', 'message': f'Marked {reg_no} as {status_val}'}), 200
    except Exception as exc:
        logger.error("Error marking attendance: %s", exc)
        return jsonify({'status': 'error', 'message': 'Failed to save attendance'}), 500


@app.route('/api/attendance/clear_all', methods=['POST'])
@teacher_login_required
def clear_all_attendance_records():
    try:
        for snap in db.collection('attendance').stream():
            snap.reference.delete()
        return jsonify({'status': 'success', 'message': 'All attendance records cleared'}), 200
    except Exception as exc:
        logger.error("Error clearing attendance records: %s", exc)
        return jsonify({'status': 'error', 'message': 'Failed to clear attendance'}), 500


# ==============================================================================
# Classroom Content APIs (Notices, Assignments, Recorded Classes, Notes, Study, Queries)
# ==============================================================================

@app.route('/api/notices', methods=['GET', 'POST'])
@authenticated_required
def notices_api():
    if request.method == 'GET':
        items = _list_collection_dicts('notices')
        items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
        return jsonify({'status': 'success', 'notices': items}), 200

    if 'teacher_id' not in session:
        return jsonify({'status': 'error', 'message': 'Only teachers can post notices'}), 403

    data = _json_body()
    title = str(data.get('title', '')).strip()
    content = str(data.get('content', '')).strip()
    if not title or not content:
        return jsonify({'status': 'error', 'message': 'Notice title and content are required'}), 400

    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    notice_id = f"notice-{uuid.uuid4().hex[:10]}"
    notice = {
        'id': notice_id,
        'title': title,
        'content': content,
        'category': str(data.get('category', 'General')).strip() or 'General',
        'priority': str(data.get('priority', 'normal')).strip() or 'normal',
        'target_audience': str(data.get('target_audience', 'all')).strip() or 'all',
        'author_id': session['teacher_id'],
        'author_name': teacher_data.get('name', 'Teacher'),
        'date': datetime.date.today().strftime('%B %d, %Y'),
        'created_at': _now_iso(),
    }
    db.collection('notices').document(notice_id).set(notice)
    return jsonify({'status': 'success', 'message': 'Notice posted successfully', 'notice': notice}), 201


@app.route('/api/notices/<notice_id>', methods=['DELETE'])
@teacher_login_required
def delete_notice_api(notice_id: str):
    doc_ref = db.collection('notices').document(notice_id)
    if not doc_ref.get().exists:
        return jsonify({'status': 'error', 'message': 'Notice not found'}), 404
    doc_ref.delete()
    return jsonify({'status': 'success', 'message': 'Notice deleted'}), 200


@app.route('/api/assignments', methods=['GET', 'POST'])
@authenticated_required
def assignments_api():
    if request.method == 'GET':
        items = _list_collection_dicts('assignments')
        submissions = _list_collection_dicts('assignment_submissions')
        items.sort(key=lambda item: item.get('created_at', ''), reverse=True)

        user_id = session.get('user_id')
        subs_by_assignment: dict[str, list[dict[str, Any]]] = {}
        for sub in submissions:
            aid = sub.get('assignment_id')
            if aid:
                subs_by_assignment.setdefault(aid, []).append(sub)

        for item in items:
            aid = item['id']
            related = subs_by_assignment.get(aid, [])
            item['submission_count'] = len(related)
            if 'teacher_id' in session:
                item['submissions'] = sorted(related, key=lambda s: s.get('submitted_at', ''), reverse=True)
            elif user_id:
                mine = next((s for s in related if s.get('student_id') == user_id), None)
                item['student_submission'] = mine
        return jsonify({'status': 'success', 'assignments': items}), 200

    if 'teacher_id' not in session:
        return jsonify({'status': 'error', 'message': 'Only teachers can post assignments'}), 403

    form_data = request.form.to_dict() if request.form else _json_body()
    title = str(form_data.get('title', '')).strip()
    subject = str(form_data.get('subject', '')).strip()
    due_date = str(form_data.get('due_date', '')).strip()
    description = str(form_data.get('description', '')).strip()
    if not title or not subject or not due_date or not description:
        return jsonify({'status': 'error', 'message': 'Title, subject, due date, and description are required'}), 400

    try:
        due_display = datetime.datetime.strptime(due_date, '%Y-%m-%d').strftime('%B %d, %Y')
    except ValueError:
        due_display = due_date

    attachment_url = ''
    attachment_name = ''
    if request.files and 'attachment' in request.files and request.files['attachment'].filename:
        try:
            rel_path, attachment_name = _save_uploaded_file(
                request.files['attachment'],
                'assignments',
                ALLOWED_MATERIAL_EXTS,
            )
            attachment_url = f"/uploads/{rel_path}"
        except ValueError as ve:
            return jsonify({'status': 'error', 'message': str(ve)}), 400

    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    assignment_id = f"assign-{uuid.uuid4().hex[:10]}"
    assignment = {
        'id': assignment_id,
        'title': title,
        'subject': subject,
        'teacher_id': session['teacher_id'],
        'teacher_name': teacher_data.get('name', 'Teacher'),
        'due_date': due_date,
        'due_display': due_display,
        'accepted_formats': str(form_data.get('accepted_formats', '.pdf,.doc,.docx,.py,.zip')).strip(),
        'description': description,
        'attachment_name': attachment_name,
        'attachment_url': attachment_url,
        'created_at': _now_iso(),
    }
    db.collection('assignments').document(assignment_id).set(assignment)
    return jsonify({'status': 'success', 'message': 'Assignment posted successfully', 'assignment': assignment}), 201


@app.route('/api/assignments/<assignment_id>', methods=['DELETE'])
@teacher_login_required
def delete_assignment_api(assignment_id: str):
    doc_ref = db.collection('assignments').document(assignment_id)
    if not doc_ref.get().exists:
        return jsonify({'status': 'error', 'message': 'Assignment not found'}), 404
    doc_ref.delete()
    return jsonify({'status': 'success', 'message': 'Assignment deleted'}), 200


@app.route('/api/assignments/<assignment_id>/submit', methods=['POST'])
@login_required
def submit_assignment_api(assignment_id: str):
    assign_doc = db.collection('assignments').document(assignment_id).get()
    if not assign_doc.exists:
        return jsonify({'status': 'error', 'message': 'Assignment not found'}), 404

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'Submission file is required'}), 400

    try:
        rel_path, original_name = _save_uploaded_file(
            request.files['file'],
            'submissions',
            ALLOWED_SUBMISSION_EXTS,
        )
    except ValueError as ve:
        return jsonify({'status': 'error', 'message': str(ve)}), 400

    user_id = session['user_id']
    student_data = _get_student_profile(user_id) or {}
    sub_id = f"{assignment_id}__{user_id}"
    submission = {
        'id': sub_id,
        'assignment_id': assignment_id,
        'student_id': user_id,
        'student_name': student_data.get('name', 'Unknown'),
        'regNo': student_data.get('regNo', ''),
        'filename': original_name,
        'file_url': f"/uploads/{rel_path}",
        'submitted_at': _now_iso(),
    }
    db.collection('assignment_submissions').document(sub_id).set(submission)
    return jsonify({
        'status': 'success',
        'message': 'Assignment submitted successfully',
        'submission': submission,
    }), 200


@app.route('/api/recorded_classes', methods=['GET', 'POST'])
@authenticated_required
def recorded_classes_api():
    if request.method == 'GET':
        items = _list_collection_dicts('recorded_classes')
        items.sort(key=lambda item: item.get('timestamp', ''), reverse=True)
        return jsonify({'status': 'success', 'classes': items}), 200

    if 'teacher_id' not in session:
        return jsonify({'status': 'error', 'message': 'Only teachers can publish recorded classes'}), 403

    form_data = request.form.to_dict() if request.form else _json_body()
    title = str(form_data.get('title', '')).strip()
    subject = str(form_data.get('subject', '')).strip()
    classroom = str(form_data.get('classroom', '')).strip() or 'Room A101'
    description = str(form_data.get('description', '')).strip()
    raw_url = str(form_data.get('videoUrl', '')).strip()

    if not title or not subject:
        return jsonify({'status': 'error', 'message': 'Class title and subject are required'}), 400

    video_url = ''
    video_type = 'embed'
    thumbnail = ''
    if request.files and 'videoFile' in request.files and request.files['videoFile'].filename:
        try:
            rel_path, _ = _save_uploaded_file(request.files['videoFile'], 'videos', ALLOWED_VIDEO_EXTS)
            video_url = f"/uploads/{rel_path}"
            video_type = 'video'
        except ValueError as ve:
            return jsonify({'status': 'error', 'message': str(ve)}), 400
    elif raw_url:
        video_url, video_type, thumbnail = _normalize_video_metadata(raw_url)
    else:
        return jsonify({'status': 'error', 'message': 'Provide a video URL or upload a video file'}), 400

    raw_attachments = form_data.get('attachments', [])
    if isinstance(raw_attachments, str):
        attachments = [part.strip() for part in raw_attachments.split(',') if part.strip()]
    elif isinstance(raw_attachments, list):
        attachments = [str(part).strip() for part in raw_attachments if str(part).strip()]
    else:
        attachments = []

    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    rec_id = f"rec-{uuid.uuid4().hex[:10]}"
    recorded_class = {
        'id': rec_id,
        'title': title,
        'subject': subject,
        'teacherName': str(form_data.get('teacherName') or teacher_data.get('name', 'Teacher')).strip(),
        'teacher_id': session['teacher_id'],
        'classroom': classroom,
        'timestamp': _now_iso(),
        'description': description,
        'videoUrl': video_url,
        'videoType': video_type,
        'thumbnail': thumbnail,
        'attachments': attachments,
    }
    db.collection('recorded_classes').document(rec_id).set(recorded_class)
    return jsonify({'status': 'success', 'message': 'Recorded class published', 'class_item': recorded_class}), 201


@app.route('/api/recorded_classes/<class_id>', methods=['DELETE'])
@teacher_login_required
def delete_recorded_class_api(class_id: str):
    doc_ref = db.collection('recorded_classes').document(class_id)
    if not doc_ref.get().exists:
        return jsonify({'status': 'error', 'message': 'Recorded class not found'}), 404
    doc_ref.delete()
    return jsonify({'status': 'success', 'message': 'Recorded class deleted'}), 200


@app.route('/api/notes', methods=['GET', 'POST'])
@authenticated_required
def notes_api():
    if request.method == 'GET':
        viewer_id = session.get('user_id') or session.get('teacher_id')
        items = _list_collection_dicts('notes')
        for item in items:
            read_by = item.get('read_by') or []
            item['isNew'] = viewer_id not in read_by
        items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
        return jsonify({'status': 'success', 'notes': items}), 200

    if 'teacher_id' not in session:
        return jsonify({'status': 'error', 'message': 'Only teachers can save class notes'}), 403

    data = _json_body()
    content = str(data.get('content') or data.get('text') or '').strip()
    if not content:
        return jsonify({'status': 'error', 'message': 'Note content cannot be empty'}), 400

    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    first_line = content.splitlines()[0].strip()
    default_title = (first_line[:60] + '...') if len(first_line) > 60 else first_line
    title = str(data.get('title') or default_title or 'Recorded Lecture Note').strip()
    subject = str(data.get('subject') or teacher_data.get('subject') or 'General').strip()
    description = str(data.get('description') or (content[:140] + ('...' if len(content) > 140 else ''))).strip()

    note_id = f"note-{uuid.uuid4().hex[:10]}"
    note = {
        'id': note_id,
        'title': title,
        'subject': subject,
        'teacher': teacher_data.get('name', 'Teacher'),
        'teacher_id': session['teacher_id'],
        'date': datetime.date.today().isoformat(),
        'description': description,
        'content': content,
        'text': content,
        'read_by': [],
        'created_at': _now_iso(),
    }
    db.collection('notes').document(note_id).set(note)
    return jsonify({'status': 'success', 'message': 'Note saved successfully', 'note': note}), 201


@app.route('/api/notes/<note_id>/read', methods=['POST'])
@authenticated_required
def mark_note_read_api(note_id: str):
    doc_ref = db.collection('notes').document(note_id)
    snap = doc_ref.get()
    if not snap.exists:
        return jsonify({'status': 'error', 'message': 'Note not found'}), 404
    note = snap.to_dict() or {}
    viewer_id = session.get('user_id') or session.get('teacher_id')
    read_by = list(note.get('read_by') or [])
    if viewer_id and viewer_id not in read_by:
        read_by.append(viewer_id)
        doc_ref.update({'read_by': read_by})
    return jsonify({'status': 'success'}), 200


@app.route('/api/notes/<note_id>', methods=['DELETE'])
@teacher_login_required
def delete_note_api(note_id: str):
    doc_ref = db.collection('notes').document(note_id)
    if not doc_ref.get().exists:
        return jsonify({'status': 'error', 'message': 'Note not found'}), 404
    doc_ref.delete()
    return jsonify({'status': 'success', 'message': 'Note deleted'}), 200


@app.route('/api/study_materials', methods=['GET', 'POST'])
@authenticated_required
def study_materials_api():
    if request.method == 'GET':
        items = _list_collection_dicts('study_materials')
        items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
        return jsonify({'status': 'success', 'materials': items}), 200

    if 'teacher_id' not in session:
        return jsonify({'status': 'error', 'message': 'Only teachers can upload study materials'}), 403

    form_data = request.form.to_dict() if request.form else _json_body()
    title = str(form_data.get('title', '')).strip()
    subject = str(form_data.get('subject', '')).strip()
    category = str(form_data.get('category', 'Lecture Notes')).strip()
    description = str(form_data.get('description', '')).strip()
    resource_url = str(form_data.get('resource_url', '')).strip()

    if not title or not subject:
        return jsonify({'status': 'error', 'message': 'Title and subject are required'}), 400

    filename = ''
    if request.files and 'file' in request.files and request.files['file'].filename:
        try:
            rel_path, filename = _save_uploaded_file(
                request.files['file'],
                'study_materials',
                ALLOWED_MATERIAL_EXTS,
            )
            resource_url = f"/uploads/{rel_path}"
        except ValueError as ve:
            return jsonify({'status': 'error', 'message': str(ve)}), 400
    elif not resource_url and not description:
        return jsonify({'status': 'error', 'message': 'Upload a file, provide a URL, or add content description'}), 400

    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    mat_id = f"mat-{uuid.uuid4().hex[:10]}"
    material = {
        'id': mat_id,
        'title': title,
        'subject': subject,
        'category': category,
        'teacher_id': session['teacher_id'],
        'teacher_name': teacher_data.get('name', 'Teacher'),
        'description': description,
        'resource_url': resource_url,
        'filename': filename,
        'created_at': _now_iso(),
    }
    db.collection('study_materials').document(mat_id).set(material)
    return jsonify({'status': 'success', 'message': 'Study material uploaded', 'material': material}), 201


@app.route('/api/study_materials/<material_id>', methods=['DELETE'])
@teacher_login_required
def delete_study_material_api(material_id: str):
    doc_ref = db.collection('study_materials').document(material_id)
    if not doc_ref.get().exists:
        return jsonify({'status': 'error', 'message': 'Study material not found'}), 404
    doc_ref.delete()
    return jsonify({'status': 'success', 'message': 'Study material deleted'}), 200


@app.route('/api/queries', methods=['GET', 'POST'])
@teacher_login_required
def queries_api():
    teacher_id = session['teacher_id']
    if request.method == 'GET':
        items = []
        for snap in db.collection('queries').where('teacher_id', '==', teacher_id).stream():
            item = snap.to_dict() or {}
            item.setdefault('id', snap.id)
            items.append(item)
        items.sort(key=lambda item: item.get('timestamp', ''), reverse=True)
        return jsonify({'status': 'success', 'queries': items}), 200

    data = _json_body()
    subject = str(data.get('subject', '')).strip()
    recipient = str(data.get('recipient', '')).strip()
    message = str(data.get('message', '')).strip()
    if not subject or not recipient or not message:
        return jsonify({'status': 'error', 'message': 'Subject, recipient, and message are required'}), 400

    teacher_data = _get_teacher_profile(teacher_id) or {}
    query_id = f"query-{uuid.uuid4().hex[:10]}"
    query_doc = {
        'id': query_id,
        'teacher_id': teacher_id,
        'teacher_name': teacher_data.get('name', 'Teacher'),
        'subject': subject,
        'recipient': recipient,
        'message': message,
        'status': 'Pending',
        'replies': [],
        'timestamp': _now_iso(),
    }
    db.collection('queries').document(query_id).set(query_doc)
    return jsonify({'status': 'success', 'message': 'Query submitted successfully', 'query': query_doc}), 201


@app.route('/api/queries/<query_id>/reply', methods=['POST'])
@teacher_login_required
def reply_query_api(query_id: str):
    doc_ref = db.collection('queries').document(query_id)
    snap = doc_ref.get()
    if not snap.exists:
        return jsonify({'status': 'error', 'message': 'Query not found'}), 404
    query_data = snap.to_dict() or {}
    if query_data.get('teacher_id') != session['teacher_id']:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    data = _json_body()
    reply_msg = str(data.get('message', '')).strip()
    if not reply_msg:
        return jsonify({'status': 'error', 'message': 'Reply message is required'}), 400

    teacher_data = _get_teacher_profile(session['teacher_id']) or {}
    replies = list(query_data.get('replies') or [])
    replies.append({
        'responder': str(data.get('responder') or teacher_data.get('name', 'Teacher')),
        'message': reply_msg,
        'timestamp': _now_iso(),
    })
    new_status = str(data.get('status') or 'In Review').strip()
    doc_ref.update({'replies': replies, 'status': new_status})
    return jsonify({'status': 'success', 'message': 'Reply added'}), 200


@app.route('/api/queries/<query_id>', methods=['DELETE'])
@teacher_login_required
def delete_query_api(query_id: str):
    doc_ref = db.collection('queries').document(query_id)
    snap = doc_ref.get()
    if not snap.exists:
        return jsonify({'status': 'error', 'message': 'Query not found'}), 404
    if (snap.to_dict() or {}).get('teacher_id') != session['teacher_id']:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    doc_ref.delete()
    return jsonify({'status': 'success', 'message': 'Query deleted'}), 200


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true', host='0.0.0.0', port=5000)