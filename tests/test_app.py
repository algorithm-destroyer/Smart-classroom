"""End-to-end and unit tests for the Smart Classroom Management System."""

from __future__ import annotations

import atexit
import base64
from html.parser import HTMLParser
import io
import os
import shutil
import sys
import tempfile
from typing import Any
from urllib.parse import urljoin

import numpy as np
from openpyxl import load_workbook
from PIL import Image
import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Ensure importing `app` during pytest collection never touches or blocks on
# live Firebase credentials or the workspace local_data directory.
_BOOT_DIR = tempfile.mkdtemp(prefix='smart_classroom_test_boot_')
atexit.register(lambda: shutil.rmtree(_BOOT_DIR, ignore_errors=True))
os.environ['CLASSROOM_STORAGE'] = 'local'
os.environ['CLASSROOM_LOCAL_DB'] = os.path.join(_BOOT_DIR, 'boot_db.json')

import app as app_module  # noqa: E402
from classroom_seed import seed_initial_data  # noqa: E402
import face_utils  # noqa: E402
import storage  # noqa: E402
from storage import LocalAuth, LocalFirestoreClient  # noqa: E402


class _HrefCollector(HTMLParser):
    """Collect anchor href attributes from rendered HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == 'a':
            for key, value in attrs:
                if key.lower() == 'href' and value is not None:
                    self.hrefs.append(value)


def _make_png_bytes(color: tuple[int, int, int] = (40, 90, 160), size: tuple[int, int] = (80, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, format='PNG')
    return buf.getvalue()


def _make_png_data_uri(color: tuple[int, int, int] = (40, 90, 160)) -> str:
    encoded = base64.b64encode(_make_png_bytes(color=color)).decode('ascii')
    return f"data:image/png;base64,{encoded}"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Give every test its own clean JSON database, uploads folder, and seeded records."""
    db_file = tmp_path / 'classroom_db.json'
    uploads_dir = tmp_path / 'uploads'
    uploads_dir.mkdir(parents=True, exist_ok=True)

    test_db = LocalFirestoreClient(str(db_file))
    test_auth = LocalAuth(test_db)
    seed_initial_data(test_db)

    monkeypatch.setattr(app_module, 'db', test_db)
    monkeypatch.setattr(app_module, 'auth', test_auth)
    monkeypatch.setattr(app_module, 'UPLOAD_DIR', str(uploads_dir))
    monkeypatch.setattr(app_module, 'BACKEND_MODE', 'local')
    monkeypatch.setattr(app_module, 'BACKEND_REASON', 'Pytest isolated local DB')
    app_module.app.config['TESTING'] = True
    return test_db, test_auth, uploads_dir


@pytest.fixture()
def client():
    with app_module.app.test_client() as test_client:
        yield test_client


def _register_student(
    client,
    *,
    reg_no: str = 'REG2025001',
    email: str = 'student1@example.com',
    name: str = 'Aarav Sharma',
    password: str = 'secret123',
):
    payload = {
        'name': name,
        'gender': 'Male',
        'dob': '2004-05-12',
        'branch': 'Computer Science',
        'year': '2nd Year',
        'email': email,
        'regNo': reg_no,
        'phone': '9876543210',
        'password': password,
    }
    return client.post('/api/register', json=payload)


def _register_teacher(
    client,
    *,
    employee_id: str = 'EMP2025001',
    email: str = 'teacher1@example.com',
    name: str = 'Dr. Meera Nair',
    password: str = 'teachpass123',
):
    payload = {
        'name': name,
        'gender': 'Female',
        'dob': '1986-08-21',
        'email': email,
        'employeeId': employee_id,
        'department': 'Computer Science',
        'subject': 'Data Structures',
        'phone': '9123456789',
        'password': password,
    }
    return client.post('/api/teacher_register', json=payload)


def _login_student(client, reg_no: str = 'REG2025001', password: str = 'secret123', remember: bool = False):
    return client.post('/api/login', json={'regNo': reg_no, 'password': password, 'remember': remember})


def _login_teacher(client, login_id: str = 'EMP2025001', password: str = 'teachpass123', remember: bool = False):
    return client.post('/api/teacher_login', json={'loginId': login_id, 'password': password, 'remember': remember})


def test_health_and_public_routes(client):
    health = client.get('/health')
    assert health.status_code == 200
    assert health.get_json()['status'] == 'ok'
    assert health.get_json()['backend_mode'] == 'local'

    for route in ('/', '/entry', '/login', '/teacher_login', '/register', '/teacher_register', '/forgot'):
        resp = client.get(route)
        assert resp.status_code == 200, f"Public route {route} failed"
        html = resp.get_data(as_text=True)
        assert '{{' not in html and '{%' not in html

    admin_redirect = client.get('/admin_login')
    assert admin_redirect.status_code == 302
    assert '/entry?notice=admin_unavailable' in admin_redirect.location

    assert client.get('/home').status_code == 302
    assert client.get('/teacher_home.html').status_code == 302
    assert client.get('/api/notices').status_code == 401
    assert client.get('/api/attendance/session').status_code == 401


def test_registration_validation_login_and_role_isolation(client):
    bad_student = client.post('/api/register', json={
        'name': '123Invalid',
        'gender': 'Male',
        'dob': '2004-05-12',
        'branch': 'CSE',
        'year': '9th Year',
        'email': 'bad-email',
        'regNo': 'REG99',
        'phone': '12345',
        'password': '123',
    })
    assert bad_student.status_code == 400

    assert _register_student(client).status_code == 200
    assert _register_student(client).status_code == 409  # Duplicate regNo/email

    assert _register_teacher(client).status_code == 200
    assert _register_teacher(client, employee_id='EMP_BAD', email='teacher1@example.com').status_code == 409

    # Role separation at login APIs
    assert _login_teacher(client, login_id='student1@example.com', password='secret123').status_code == 404
    assert _login_student(client, reg_no='EMP2025001', password='teachpass123').status_code == 404

    # Wrong password rejected
    assert _login_student(client, password='wrong-password').status_code == 401

    # Valid student login with remember=True
    res = _login_student(client, remember=True)
    assert res.status_code == 200
    assert client.get('/home').status_code == 200
    assert client.get('/teacher_home.html').status_code == 302
    assert client.post('/api/notices', json={'title': 'X', 'content': 'Y'}).status_code in (401, 403)

    # Teacher login works with email or employeeId
    assert _login_teacher(client, login_id='teacher1@example.com').status_code == 200
    assert client.get('/teacher_home.html').status_code == 200
    assert client.get('/home').status_code == 302
    assert client.post('/api/generate_report_card', json={'exam_name': 'Final Examination'}).status_code in (401, 403)
    assert _login_teacher(client, login_id='EMP2025001').status_code == 200


def test_all_rendered_routes_and_internal_link_integrity(client):
    assert _register_student(client).status_code == 200
    assert _register_teacher(client).status_code == 200

    public_routes = ['/', '/entry', '/login', '/teacher_login', '/register', '/teacher_register', '/forgot']
    student_routes = [
        '/home',
        '/attendance.html',
        '/assignments.html',
        '/recorded_class.html',
        '/notes.html',
        '/notices.html',
        '/profile.html',
        '/settings.html',
        '/academic_profile.html',
    ]
    teacher_routes = [
        '/teacher_home.html',
        '/teacher_settings.html',
        '/teacher_fa.html',
        '/teacher_assignment.html',
        '/teacher_notice.html',
        '/teacher_recorded_class.html',
        '/teacher_query.html',
        '/teacher_study.html',
        '/teacher_analytics.html',
        '/teacher_attendance.html',
        '/teacher_profile.html',
        '/teacher_notes.html',
    ]

    # Alias redirects
    assert _login_student(client).status_code == 200
    assert client.get('/home.html').status_code == 302
    assert client.get('/assignment.html').status_code == 302

    def audit_pages(routes: list[str]) -> set[str]:
        discovered_targets: set[str] = set()
        for route in routes:
            resp = client.get(route)
            assert resp.status_code == 200, f"Route {route} returned {resp.status_code}"
            html = resp.get_data(as_text=True)
            assert '{{' not in html and '{%' not in html, f"Unrendered Jinja syntax in {route}"
            assert 'student home.html' not in html, f"Legacy broken link in {route}"
            parser = _HrefCollector()
            parser.feed(html)
            for href in parser.hrefs:
                if not href or href.startswith(('#', 'http://', 'https://', 'mailto:', 'tel:', 'javascript:')):
                    continue
                resolved = urljoin(f"http://localhost{route}", href)
                path = resolved.split('http://localhost', 1)[-1].split('?', 1)[0]
                discovered_targets.add(path)
        return discovered_targets

    public_links = audit_pages(public_routes)
    for target in public_links:
        assert client.get(target).status_code in (200, 302), f"Public link {target} broken"

    student_links = audit_pages(student_routes)
    for target in student_links:
        resp = client.get(target)
        assert resp.status_code in (200, 302), f"Student link {target} failed with {resp.status_code}"

    assert _login_teacher(client).status_code == 200
    fa_redirect = client.get('/fa.html')
    assert fa_redirect.status_code == 302
    assert '/teacher_fa.html' in fa_redirect.location

    teacher_links = audit_pages(teacher_routes)
    for target in teacher_links:
        resp = client.get(target)
        assert resp.status_code in (200, 302), f"Teacher link {target} failed with {resp.status_code}"


def test_account_settings_password_profile_and_contact(client):
    assert _register_student(client).status_code == 200
    assert _register_teacher(client).status_code == 200

    # Contact endpoint
    assert client.post('/api/contact', json={'name': '', 'email': 'bad', 'message': ''}).status_code == 400
    contact_res = client.post('/api/contact', json={
        'name': 'Parent',
        'email': 'parent@example.com',
        'message': 'Need timetable update.',
    })
    assert contact_res.status_code == 200

    # Student profile photo + settings + password change
    assert _login_student(client).status_code == 200
    photo_res = client.post(
        '/api/student/profile/photo',
        data={'photo': (io.BytesIO(_make_png_bytes()), 'avatar.png')},
        content_type='multipart/form-data',
    )
    assert photo_res.status_code == 200
    assert photo_res.get_json()['profile_photo'].startswith('data:image/jpeg;base64,')

    assert client.post('/api/update_settings', json={'email_notifications': False}).status_code == 200
    assert client.post('/api/change_password', json={
        'current_password': 'wrong',
        'new_password': 'newpassword1',
    }).status_code == 401
    assert client.post('/api/change_password', json={
        'current_password': 'secret123',
        'new_password': 'newpassword1',
    }).status_code == 200

    # Sign out clears session, forgot password works
    assert client.post('/api/sign_out').status_code == 200
    assert client.get('/home').status_code == 302
    assert client.post('/api/forgot_password', json={'email': 'student1@example.com'}).status_code == 200
    assert client.post('/api/forgot_password', json={'email': 'nobody@example.com'}).status_code == 404

    # Teacher profile update (PUT with photo + fields) & account deletion
    assert _login_teacher(client).status_code == 200
    assert client.post('/api/update_settings', json={'email_notifications': True}).status_code == 200
    bad_update = client.put('/api/teacher/profile', json={'phone': '999'})
    assert bad_update.status_code == 400

    profile_put = client.put(
        '/api/teacher/profile',
        data={
            'bio': 'Updated systems faculty bio',
            'office_location': 'Block B - 204',
            'photo': (io.BytesIO(_make_png_bytes((10, 120, 40))), 'faculty.png'),
        },
        content_type='multipart/form-data',
    )
    assert profile_put.status_code == 200
    profile_json = profile_put.get_json()['profile']
    assert profile_json['bio'] == 'Updated systems faculty bio'
    assert profile_json['office_location'] == 'Block B - 204'
    assert profile_json['profile_photo'].startswith('data:image/jpeg;base64,')

    # Delete account clears session & removes auth
    assert client.post('/api/delete_account').status_code == 200
    assert client.get('/teacher_home.html').status_code == 302
    assert _login_teacher(client).status_code == 404


def test_classroom_crud_end_to_end(client):
    assert _register_student(client).status_code == 200
    assert _register_teacher(client, employee_id='EMP1', email='t1@example.com').status_code == 200
    assert _register_teacher(client, employee_id='EMP2', email='t2@example.com', name='Prof. Vikram Rao').status_code == 200

    assert _login_teacher(client, login_id='EMP1').status_code == 200

    # Notices CRUD
    notice_create = client.post('/api/notices', json={
        'title': 'Lab Exam Venue',
        'category': 'Examination',
        'priority': 'high',
        'content': 'CS Lab 2 at 10:00 AM',
    })
    assert notice_create.status_code == 201
    notice_id = notice_create.get_json()['notice']['id']
    notices_list = client.get('/api/notices').get_json()['notices']
    assert any(n['id'] == notice_id for n in notices_list)

    # Assignment creation + student submission + attachment download
    assign_create = client.post(
        '/api/assignments',
        data={
            'title': 'Compiler Design Lab',
            'subject': 'Compilers',
            'due_date': '2026-12-01',
            'description': 'Write lexical analyzer.',
            'attachment': (io.BytesIO(b'%PDF-1.4 prompt'), 'prompt.pdf'),
        },
        content_type='multipart/form-data',
    )
    assert assign_create.status_code == 201
    assignment = assign_create.get_json()['assignment']
    assert assignment['attachment_url'].startswith('/uploads/assignments/')
    dl_prompt = client.get(assignment['attachment_url'])
    assert dl_prompt.status_code == 200
    assert b'%PDF-1.4 prompt' in dl_prompt.data

    # Recorded classes: YouTube normalization & uploaded MP4
    yt_res = client.post('/api/recorded_classes', json={
        'title': 'Trees & Graphs',
        'subject': 'Data Structures',
        'classroom': 'Room 101',
        'videoUrl': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        'description': 'BFS and DFS walkthrough',
    })
    assert yt_res.status_code == 201
    yt_cls = yt_res.get_json()['class_item']
    assert yt_cls['videoType'] == 'embed'
    assert yt_cls['videoUrl'] == 'https://www.youtube.com/embed/dQw4w9WgXcQ'
    assert 'dQw4w9WgXcQ' in yt_cls['thumbnail']

    mp4_res = client.post(
        '/api/recorded_classes',
        data={
            'title': 'Pointer Arithmetic Demo',
            'subject': 'C Programming',
            'classroom': 'Lab A',
            'videoFile': (io.BytesIO(b'fake-mp4-stream'), 'lecture.mp4'),
        },
        content_type='multipart/form-data',
    )
    assert mp4_res.status_code == 201
    mp4_cls = mp4_res.get_json()['class_item']
    assert mp4_cls['videoType'] == 'video'
    assert mp4_cls['videoUrl'].startswith('/uploads/videos/')

    # Study materials + notes + queries
    mat_res = client.post(
        '/api/study_materials',
        data={
            'title': 'Week 1 Slides',
            'subject': 'Data Structures',
            'category': 'Slides',
            'description': 'Complexity cheatsheet',
            'file': (io.BytesIO(b'hello notes'), 'slides.txt'),
        },
        content_type='multipart/form-data',
    )
    assert mat_res.status_code == 201
    mat = mat_res.get_json()['material']
    assert client.get(mat['resource_url']).status_code == 200

    note_res = client.post('/api/notes', json={'text': 'Dynamic programming memoization summary'})
    assert note_res.status_code == 201
    note_id = note_res.get_json()['note']['id']

    query_res = client.post('/api/queries', json={
        'subject': 'Projector Issue',
        'recipient': 'IT Support',
        'message': 'Room 101 HDMI cable fault.',
    })
    assert query_res.status_code == 201
    query_id = query_res.get_json()['query']['id']
    assert client.post(f'/api/queries/{query_id}/reply', json={'message': 'Ticket logged'}).status_code == 200
    assert client.get('/api/queries').get_json()['queries'][0]['status'] == 'In Review'

    # Other teacher cannot reply or delete teacher 1's query
    assert _login_teacher(client, login_id='EMP2').status_code == 200
    assert client.post(f'/api/queries/{query_id}/reply', json={'message': 'Hack'}).status_code == 403
    assert client.delete(f'/api/queries/{query_id}').status_code == 403

    # Student reads note, submits assignment, sees materials/classes/notices
    assert _login_student(client).status_code == 200
    notes_before = client.get('/api/notes').get_json()['notes']
    target_note = next(n for n in notes_before if n['id'] == note_id)
    assert target_note['isNew'] is True
    assert client.post(f'/api/notes/{note_id}/read').status_code == 200
    notes_after = client.get('/api/notes').get_json()['notes']
    assert next(n for n in notes_after if n['id'] == note_id)['isNew'] is False

    sub_res = client.post(
        f"/api/assignments/{assignment['id']}/submit",
        data={'file': (io.BytesIO(b'print("lexer")\n'), 'solution.py')},
        content_type='multipart/form-data',
    )
    assert sub_res.status_code == 200
    student_assigns = client.get('/api/assignments').get_json()['assignments']
    mine = next(a for a in student_assigns if a['id'] == assignment['id'])
    assert mine['student_submission']['filename'] == 'solution.py'

    assert client.get('/api/recorded_classes').status_code == 200
    assert client.get('/api/study_materials').status_code == 200

    # Teacher sees submission count and cleans up created resources
    assert _login_teacher(client, login_id='EMP1').status_code == 200
    teacher_assigns = client.get('/api/assignments').get_json()['assignments']
    created_assign = next(a for a in teacher_assigns if a['id'] == assignment['id'])
    assert created_assign['submission_count'] == 1

    assert client.delete(f'/api/notices/{notice_id}').status_code == 200
    assert client.delete(f"/api/assignments/{assignment['id']}").status_code == 200
    assert client.delete(f"/api/recorded_classes/{yt_cls['id']}").status_code == 200
    assert client.delete(f"/api/recorded_classes/{mp4_cls['id']}").status_code == 200
    assert client.delete(f"/api/study_materials/{mat['id']}").status_code == 200
    assert client.delete(f'/api/notes/{note_id}').status_code == 200
    assert client.delete(f'/api/queries/{query_id}').status_code == 200


def test_attendance_session_analytics_and_report_card(client):
    assert _register_student(client, reg_no='REG001', email='s1@example.com', name='Aarav Sharma').status_code == 200
    assert _register_teacher(client).status_code == 200

    assert _login_teacher(client).status_code == 200
    # Stop session -> recognize_face blocked with 403
    stop_res = client.post('/api/attendance/session', json={'action': 'stop'})
    assert stop_res.status_code == 200
    assert stop_res.get_json()['session']['active'] is False

    assert _login_student(client, reg_no='REG001').status_code == 200
    sess_check = client.get('/api/attendance/session').get_json()['session']
    assert sess_check['active'] is False
    assert client.post('/api/recognize_face', json={'image': _make_png_data_uri()}).status_code == 403

    # Start session and mark attendance manually
    assert _login_teacher(client).status_code == 200
    start_res = client.post('/api/attendance/session', json={'action': 'start', 'hour': '3rd Hour'})
    assert start_res.status_code == 200
    assert start_res.get_json()['session']['active'] is True
    assert start_res.get_json()['session']['hour'] == '3rd Hour'

    mark_res = client.post('/api/attendance/mark', json={'regNo': 'REG001', 'status': 'Present'})
    assert mark_res.status_code == 200

    records_res = client.get('/api/attendance/records')
    assert records_res.status_code == 200
    rec_json = records_res.get_json()
    assert len(rec_json['records']) >= 1
    assert rec_json['students'][0]['regNo'] == 'REG001'
    assert any(r['student_id'] == 'REG001' and r['status'] == 'Present' for r in rec_json['records'])

    # Date-range filtering excludes out-of-range dates
    empty_range = client.get('/api/attendance/records?from_date=2099-01-01&to_date=2099-12-31').get_json()
    assert empty_range['records'] == []

    today_res = client.get('/api/get_today_attendance')
    assert today_res.status_code == 200
    today_map = today_res.get_json()['attendance']
    assert today_map['REG001']['status'] == 'Present'

    # Analytics helper + rendered analytics page
    students_list, analytics = app_module._compute_analytics_data()
    assert len(students_list) == 1
    assert analytics['total_students'] == 1
    assert analytics['avg_attendance'] == 100.0
    analytics_page = client.get('/teacher_analytics.html')
    assert analytics_page.status_code == 200
    assert 'Aarav Sharma' in analytics_page.get_data(as_text=True)

    # Clear attendance endpoints
    assert client.post('/api/reset_today_attendance').status_code == 200
    assert client.get('/api/get_today_attendance').get_json()['attendance'] == {}
    assert client.post('/api/attendance/clear_all').status_code == 200

    # Student XLSX report card generation
    assert _login_student(client, reg_no='REG001').status_code == 200
    assert client.post('/api/generate_report_card', json={'exam_name': ''}).status_code == 400
    report_res = client.post('/api/generate_report_card', json={'exam_name': 'Final Examination'})
    assert report_res.status_code == 200
    wb = load_workbook(io.BytesIO(report_res.data))
    ws = wb['Report Card']
    assert ws['B2'].value == 'Aarav Sharma'
    assert ws['B3'].value == 'REG001'
    assert ws['B4'].value == 'Final Examination'
    assert ws['A8'].value == 'Mathematics'


def test_face_registration_recognition_and_utils(client, monkeypatch):
    assert _register_student(client, reg_no='FACE01', email='face1@example.com', name='Diya Menon').status_code == 200
    assert _register_teacher(client).status_code == 200

    # Deterministic 128D vectors so synthetic image doesn't require real human portrait geometry
    vec_a = np.zeros(128, dtype=np.float32)
    vec_a[0] = 1.0
    vec_b = np.zeros(128, dtype=np.float32)
    vec_b[1] = 1.0
    monkeypatch.setattr(app_module, 'get_face_encoding', lambda img: vec_a if 'A' in str(img) else vec_b)

    assert _login_teacher(client).status_code == 200
    assert client.post('/api/register_face', json={'regNo': 'FACE01', 'image': 'imgA'}).status_code == 200
    reg_list = client.get('/api/get_registered_students').get_json()['students']
    assert len(reg_list) == 1 and reg_list[0]['regNo'] == 'FACE01'

    # Unmatching vector rejected, matching vector marks attendance, duplicate blocked
    assert client.post('/api/recognize_face', json={'image': 'imgB'}).status_code == 400
    ok_recog = client.post('/api/recognize_face', json={'image': 'imgA'})
    assert ok_recog.status_code == 200
    assert ok_recog.get_json()['studentId'] == 'FACE01'
    assert client.post('/api/recognize_face', json={'image': 'imgA'}).status_code == 400  # Already marked

    assert client.post('/api/delete_face', json={'regNo': 'FACE01'}).status_code == 200
    assert client.get('/api/get_registered_students').get_json()['students'] == []
    assert client.post('/api/register_face', json={'regNo': 'FACE01', 'image': 'imgA'}).status_code == 200
    assert client.post('/api/clear_all_faces').status_code == 200
    assert client.get('/api/get_registered_students').get_json()['students'] == []

    # Direct face_utils coverage with OpenCV Haar cascade mocked
    decoded = face_utils.decode_base64_image(_make_png_data_uri())
    assert decoded.shape[2] == 3
    assert face_utils.match_face_encoding([vec_a, vec_b], vec_a)[0] == 0
    assert face_utils.match_face_encoding([vec_b], vec_a)[0] is None

    class _FakeCascade:
        def detectMultiScale(self, *args, **kwargs):
            return [(10, 10, 40, 40)]

    monkeypatch.setattr(face_utils, '_FACE_CASCADE', _FakeCascade())
    monkeypatch.setattr(face_utils, 'HAS_FACE_RECOGNITION', False)
    cv_vec = face_utils.get_face_encoding(_make_png_data_uri())
    assert cv_vec.shape == (128,)


def test_storage_backend_fallback_and_mocked_firebase(tmp_path, monkeypatch):
    # Auto fallback when credential file is absent
    db_path = tmp_path / 'fallback.json'
    _, _, mode, reason = storage.create_backend(
        service_account_path=str(tmp_path / 'missing_key.json'),
        storage_mode='auto',
        local_db_path=str(db_path),
    )
    assert mode == 'local'
    assert 'fallback' in reason.lower()
    assert db_path.exists()

    # Mocked live Firebase path returns 'firebase'
    key_file = tmp_path / 'dummy_key.json'
    key_file.write_text('{}', encoding='utf-8')

    class _FakeCredObj:
        def get_credential(self):
            class _Refreshable:
                def refresh(self, _req):
                    return None
            return _Refreshable()

    fake_firestore_client = object()
    monkeypatch.setattr(storage.credentials, 'Certificate', lambda path: _FakeCredObj())
    monkeypatch.setattr(storage, 'get_app', lambda: object())
    monkeypatch.setattr(storage.firestore, 'client', lambda: fake_firestore_client)

    fb_db, fb_auth_mod, fb_mode, _ = storage.create_backend(
        service_account_path=str(key_file),
        storage_mode='auto',
        local_db_path=str(db_path),
    )
    assert fb_mode == 'firebase'
    assert fb_db is fake_firestore_client
    assert fb_auth_mod is storage.fb_auth


def test_uploads_rejects_traversal_and_missing_files(client):
    assert _register_student(client).status_code == 200
    assert _login_student(client).status_code == 200
    assert client.get('/uploads/../app.py').status_code in (400, 404)
    assert client.get('/uploads/nonexistent/file.pdf').status_code == 404
