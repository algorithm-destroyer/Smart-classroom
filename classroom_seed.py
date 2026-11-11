"""Initial dataset seeding for empty classroom collections.

Ensures a fresh local or Firebase deployment immediately displays coherent notices,
assignments, notes, recorded classes, study materials, and an active attendance
session without relying on hardcoded HTML stubs. Runs at most once via ``_meta/seed_v1``.
"""

from __future__ import annotations

import datetime
from typing import Any

DEFAULT_NOTICES = [
    {
        'id': 'notice-1',
        'title': 'Mid-Term Examination Schedule',
        'date': 'October 20, 2025',
        'category': 'Examination',
        'priority': 'high',
        'target_audience': 'all',
        'author_name': 'Examination Cell',
        'content': 'The mid-term examinations will commence from November 1, 2025. Please check the detailed schedule on the notice board.',
        'created_at': '2025-10-20T09:00:00',
    },
    {
        'id': 'notice-2',
        'title': 'Library Extended Hours',
        'date': 'October 18, 2025',
        'category': 'General',
        'priority': 'normal',
        'target_audience': 'students',
        'author_name': 'Chief Librarian',
        'content': 'The library will remain open until 10:00 PM during the examination period from October 25 to November 15.',
        'created_at': '2025-10-18T10:00:00',
    },
    {
        'id': 'notice-3',
        'title': 'Workshop on AI and Machine Learning',
        'date': 'October 15, 2025',
        'category': 'Event',
        'priority': 'normal',
        'target_audience': 'all',
        'author_name': 'Dept. of Computer Science',
        'content': 'Department of Computer Science is organizing a workshop on AI and Machine Learning on October 30, 2025. Registration is mandatory.',
        'created_at': '2025-10-15T11:30:00',
    },
    {
        'id': 'notice-4',
        'title': 'Project Submission Deadline Extended',
        'date': 'October 5, 2025',
        'category': 'Academic',
        'priority': 'high',
        'target_audience': 'students',
        'author_name': 'Academic Coordinator',
        'content': 'The deadline for submitting the group project is extended to October 30, 2025. Submit via the portal.',
        'created_at': '2025-10-05T14:00:00',
    },
]

DEFAULT_ASSIGNMENTS = [
    {
        'id': 'assign-1',
        'title': 'Database Management Systems - Assignment 1',
        'subject': 'Database Management Systems',
        'teacher_name': 'Prof. Smith',
        'due_date': '2025-10-30',
        'due_display': 'October 30, 2025',
        'accepted_formats': '.pdf,.doc,.docx',
        'description': 'Complete the ER diagram for the given case study and write SQL queries for the provided dataset.',
        'created_at': '2025-10-10T09:00:00',
    },
    {
        'id': 'assign-2',
        'title': 'Data Structures - Assignment 2',
        'subject': 'Data Structures',
        'teacher_name': 'Dr. Jones',
        'due_date': '2025-11-05',
        'due_display': 'November 5, 2025',
        'accepted_formats': '.py,.zip',
        'description': 'Implement a binary search tree with insert, delete, and search operations in Python.',
        'created_at': '2025-10-12T11:00:00',
    },
    {
        'id': 'assign-3',
        'title': 'Operating Systems - Assignment 3',
        'subject': 'Operating Systems',
        'teacher_name': 'Prof. Lee',
        'due_date': '2025-11-10',
        'due_display': 'November 10, 2025',
        'accepted_formats': '.pdf,.doc,.docx',
        'description': 'Analyze the given scheduling algorithms and provide a comparative study.',
        'created_at': '2025-10-15T15:00:00',
    },
]

DEFAULT_NOTES = [
    {
        'id': 'note-1',
        'title': 'Introduction to Algebra',
        'subject': 'Mathematics',
        'teacher': 'Dr. Smith',
        'date': '2025-09-15',
        'description': 'Basic concepts of algebra including variables, expressions, and equations.',
        'content': (
            'Algebra is a branch of mathematics dealing with symbols and the rules for manipulating those symbols.\n\n'
            'Key Concepts:\n1. Variables and Constants\n2. Expressions and Equations\n'
            '3. Solving Linear Equations\n4. Graphing Linear Functions'
        ),
        'read_by': [],
        'created_at': '2025-09-15T10:00:00',
    },
    {
        'id': 'note-2',
        'title': 'Cell Structure and Function',
        'subject': 'Science',
        'teacher': 'Prof. Johnson',
        'date': '2025-09-18',
        'description': 'Overview of cell structure and function in living organisms.',
        'content': (
            'Cells are the basic building blocks of all living things.\n\n'
            'Cell Theory:\n1. All living organisms are composed of one or more cells.\n'
            '2. The cell is the basic unit of structure in organisms.\n'
            '3. All cells arise from pre-existing cells.'
        ),
        'read_by': [],
        'created_at': '2025-09-18T11:00:00',
    },
    {
        'id': 'note-3',
        'title': 'Introduction to Python Programming',
        'subject': 'Computer Science',
        'teacher': 'Mr. Miller',
        'date': '2025-09-28',
        'description': 'Introduction to Python programming language with basic syntax and examples.',
        'content': (
            'Python is a high-level, interpreted programming language known for its simplicity and readability.\n\n'
            'Topics Covered:\n- Variables and Data Types\n- Lists and Dictionaries\n'
            '- Conditional Statements\n- Loops and Functions'
        ),
        'read_by': [],
        'created_at': '2025-09-28T13:00:00',
    },
]

DEFAULT_RECORDED_CLASSES = [
    {
        'id': 'rec-1',
        'title': 'Introduction to Algebra',
        'subject': 'Mathematics',
        'teacherName': 'Prof. Smith',
        'classroom': 'Room A101',
        'timestamp': '2025-09-20T10:00:00Z',
        'description': 'Learn the basics of algebra, including variables and equations.',
        'videoUrl': 'https://www.youtube.com/embed/9vKqVkMQHKk',
        'videoType': 'embed',
        'thumbnail': 'https://img.youtube.com/vi/9vKqVkMQHKk/mqdefault.jpg',
        'attachments': ['Algebra_Notes.pdf', 'Practice_Problems.docx'],
    },
    {
        'id': 'rec-2',
        'title': 'Photosynthesis Basics',
        'subject': 'Science',
        'teacherName': 'Dr. Green',
        'classroom': 'Room B202',
        'timestamp': '2025-09-18T14:00:00Z',
        'description': 'Understand the process of photosynthesis in plants.',
        'videoUrl': 'https://www.youtube.com/embed/uixA8ZXx0KU',
        'videoType': 'embed',
        'thumbnail': 'https://img.youtube.com/vi/uixA8ZXx0KU/mqdefault.jpg',
        'attachments': ['Photosynthesis_Slides.pdf'],
    },
    {
        'id': 'rec-3',
        'title': 'Python Programming Basics',
        'subject': 'Computer Science',
        'teacherName': 'Dr. Lee',
        'classroom': 'Room E505',
        'timestamp': '2025-09-22T13:00:00Z',
        'description': 'Introduction to Python programming for beginners.',
        'videoUrl': 'https://www.youtube.com/embed/rfscVS0vtbw',
        'videoType': 'embed',
        'thumbnail': 'https://img.youtube.com/vi/rfscVS0vtbw/mqdefault.jpg',
        'attachments': ['Python_Code_Examples.zip'],
    },
]

DEFAULT_STUDY_MATERIALS = [
    {
        'id': 'mat-1',
        'title': 'Database Normalization Quick Reference',
        'subject': 'Computer Science',
        'category': 'Reference Guide',
        'teacher_name': 'Prof. Smith',
        'description': 'Concise walkthrough of 1NF, 2NF, 3NF, and BCNF with worked examples.',
        'resource_url': '',
        'filename': '',
        'created_at': '2025-09-25T09:00:00',
    },
    {
        'id': 'mat-2',
        'title': 'Calculus Formula Sheet',
        'subject': 'Mathematics',
        'category': 'Formula Sheet',
        'teacher_name': 'Dr. Alan',
        'description': 'Derivatives, integrals, and series expansion identities for mid-term preparation.',
        'resource_url': '',
        'filename': '',
        'created_at': '2025-09-24T15:30:00',
    },
]


def seed_initial_data(db: Any) -> None:
    """Populate default classroom records once if collections are empty."""
    meta_ref = db.collection('_meta').document('seed_v1')
    if meta_ref.get().exists:
        return

    collections_to_seed = [
        ('notices', DEFAULT_NOTICES),
        ('assignments', DEFAULT_ASSIGNMENTS),
        ('notes', DEFAULT_NOTES),
        ('recorded_classes', DEFAULT_RECORDED_CLASSES),
        ('study_materials', DEFAULT_STUDY_MATERIALS),
    ]

    batch = db.batch()
    has_writes = False

    for col_name, items in collections_to_seed:
        col_ref = db.collection(col_name)
        has_any = next(col_ref.limit(1).stream(), None) is not None
        if not has_any:
            for item in items:
                batch.set(col_ref.document(item['id']), dict(item))
                has_writes = True

    session_ref = db.collection('attendance_sessions').document('current')
    if not session_ref.get().exists:
        batch.set(session_ref, {
            'active': True,
            'hour': '1st Hour',
            'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'started_by': 'System Default',
        })
        has_writes = True

    batch.set(meta_ref, {
        'seeded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    if has_writes or not meta_ref.get().exists:
        batch.commit()
