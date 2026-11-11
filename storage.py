"""Storage and authentication adapters for Smart Classroom.

Provides a unified Firestore- and Firebase-Auth-compatible interface backed either
by live Firebase (when valid credentials are available and reachable) or by a
thread-safe local JSON store with Werkzeug password hashing when running offline
or with missing/revoked credentials.
"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import threading
import uuid
from typing import Any

from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore, get_app, initialize_app
from google.auth.transport.requests import Request as GoogleAuthRequest
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)


class LocalUserNotFoundError(Exception):
    """Raised when an authentication user record does not exist."""


class LocalAuthError(Exception):
    """Raised for email/password authentication failures in local mode."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _LocalUserRecord:
    """Minimal Firebase Auth UserRecord equivalent."""

    def __init__(self, uid: str, email: str, custom_claims: dict[str, Any] | None = None):
        self.uid = uid
        self.email = email
        self.custom_claims = custom_claims or {}


class LocalDocumentSnapshot:
    """Firestore DocumentSnapshot equivalent backed by a plain dictionary."""

    def __init__(self, doc_id: str, data: dict[str, Any] | None, reference: LocalDocumentReference):
        self.id = doc_id
        self._data = copy.deepcopy(data)
        self.reference = reference
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        """Return a deep copy of document fields or None if missing."""
        return copy.deepcopy(self._data) if self._data is not None else None


class LocalWriteBatch:
    """Batch writer implementing set, update, delete, and commit."""

    def __init__(self, client: LocalFirestoreClient):
        self._client = client
        self._ops: list[tuple[str, LocalDocumentReference, dict[str, Any] | None]] = []

    def set(self, doc_ref: LocalDocumentReference, data: dict[str, Any]) -> None:
        self._ops.append(('set', doc_ref, data))

    def update(self, doc_ref: LocalDocumentReference, data: dict[str, Any]) -> None:
        self._ops.append(('update', doc_ref, data))

    def delete(self, doc_ref: LocalDocumentReference) -> None:
        self._ops.append(('delete', doc_ref, None))

    def commit(self) -> None:
        with self._client._lock:
            store = self._client._load_unlocked()
            collections = store.setdefault('collections', {})
            for op, ref, payload in self._ops:
                col = collections.setdefault(ref.collection_name, {})
                if op == 'set':
                    col[ref.id] = _serialize_value(copy.deepcopy(payload or {}))
                elif op == 'update':
                    existing = col.setdefault(ref.id, {})
                    _apply_update(existing, payload or {})
                elif op == 'delete':
                    col.pop(ref.id, None)
            self._client._save_unlocked(store)
        self._ops.clear()


class LocalQuery:
    """Simple query filter and limit wrapper for LocalCollection."""

    def __init__(
        self,
        collection: LocalCollection,
        filters: list[tuple[str, str, Any]] | None = None,
        limit_count: int | None = None,
    ):
        self._collection = collection
        self._filters = filters or []
        self._limit = limit_count

    def where(self, field: str, op: str, value: Any) -> LocalQuery:
        return LocalQuery(
            self._collection,
            filters=[*self._filters, (field, op, value)],
            limit_count=self._limit,
        )

    def limit(self, count: int) -> LocalQuery:
        return LocalQuery(self._collection, filters=self._filters, limit_count=count)

    def stream(self):
        snapshots = self._collection._matching_snapshots(self._filters)
        if self._limit is not None:
            snapshots = snapshots[: self._limit]
        return iter(snapshots)


class LocalDocumentReference:
    """Firestore DocumentReference equivalent."""

    def __init__(self, client: LocalFirestoreClient, collection_name: str, doc_id: str):
        self._client = client
        self.collection_name = collection_name
        self.id = doc_id

    def get(self) -> LocalDocumentSnapshot:
        with self._client._lock:
            store = self._client._load_unlocked()
            raw = store.get('collections', {}).get(self.collection_name, {}).get(self.id)
            data = _deserialize_value(raw) if raw is not None else None
            return LocalDocumentSnapshot(self.id, data, self)

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        with self._client._lock:
            store = self._client._load_unlocked()
            col = store.setdefault('collections', {}).setdefault(self.collection_name, {})
            if merge and self.id in col and isinstance(col[self.id], dict):
                _apply_update(col[self.id], data)
            else:
                col[self.id] = _serialize_value(copy.deepcopy(data))
            self._client._save_unlocked(store)

    def update(self, data: dict[str, Any]) -> None:
        with self._client._lock:
            store = self._client._load_unlocked()
            col = store.setdefault('collections', {}).setdefault(self.collection_name, {})
            if self.id not in col:
                raise KeyError(f"Document {self.collection_name}/{self.id} does not exist")
            _apply_update(col[self.id], data)
            self._client._save_unlocked(store)

    def delete(self) -> None:
        with self._client._lock:
            store = self._client._load_unlocked()
            store.get('collections', {}).get(self.collection_name, {}).pop(self.id, None)
            self._client._save_unlocked(store)


class LocalCollection:
    """Firestore CollectionReference equivalent."""

    def __init__(self, client: LocalFirestoreClient, name: str):
        self._client = client
        self.id = name

    def document(self, doc_id: str | None = None) -> LocalDocumentReference:
        if not doc_id:
            doc_id = uuid.uuid4().hex
        return LocalDocumentReference(self._client, self.id, doc_id)

    def where(self, field: str, op: str, value: Any) -> LocalQuery:
        return LocalQuery(self, filters=[(field, op, value)])

    def limit(self, count: int) -> LocalQuery:
        return LocalQuery(self, limit_count=count)

    def stream(self):
        return iter(self._matching_snapshots([]))

    def _matching_snapshots(self, filters: list[tuple[str, str, Any]]) -> list[LocalDocumentSnapshot]:
        with self._client._lock:
            store = self._client._load_unlocked()
            docs = store.get('collections', {}).get(self.id, {})
            result: list[LocalDocumentSnapshot] = []
            for doc_id, raw_data in docs.items():
                data = _deserialize_value(raw_data)
                if _matches_filters(data, filters):
                    ref = LocalDocumentReference(self._client, self.id, doc_id)
                    result.append(LocalDocumentSnapshot(doc_id, data, ref))
            return result


class LocalFirestoreClient:
    """JSON-persisted Firestore-compatible client for offline/local execution."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(db_path):
            with self._lock:
                self._save_unlocked({'collections': {}, 'auth_users': {}})

    def collection(self, name: str) -> LocalCollection:
        return LocalCollection(self, name)

    def batch(self) -> LocalWriteBatch:
        return LocalWriteBatch(self)

    def _load_unlocked(self) -> dict[str, Any]:
        if not os.path.exists(self.db_path):
            return {'collections': {}, 'auth_users': {}}
        try:
            with open(self.db_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {'collections': {}, 'auth_users': {}}
            data.setdefault('collections', {})
            data.setdefault('auth_users', {})
            return data
        except (json.JSONDecodeError, OSError):
            return {'collections': {}, 'auth_users': {}}

    def _save_unlocked(self, store: dict[str, Any]) -> None:
        import time

        tmp_path = f"{self.db_path}.{uuid.uuid4().hex}.tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(store, f, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp_path, self.db_path)
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.02 * (attempt + 1))
        # Fallback if external file indexer/OneDrive momentarily blocks atomic replace
        try:
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(store, f, indent=2)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


class LocalAuth:
    """Firebase Auth-compatible credential store backed by LocalFirestoreClient."""

    UserNotFoundError = LocalUserNotFoundError

    def __init__(self, client: LocalFirestoreClient):
        self._client = client

    def create_user(self, email: str, password: str) -> _LocalUserRecord:
        if len(password or '') < 6:
            raise ValueError("WEAK_PASSWORD: Password must be at least 6 characters")
        normalized_email = (email or '').strip().lower()
        with self._client._lock:
            store = self._client._load_unlocked()
            users = store.setdefault('auth_users', {})
            for record in users.values():
                if (record.get('email') or '').lower() == normalized_email:
                    raise ValueError("EMAIL_EXISTS: The email address is already in use")
            uid = uuid.uuid4().hex
            users[uid] = {
                'uid': uid,
                'email': email.strip(),
                'password_hash': generate_password_hash(password),
                'custom_claims': {},
            }
            self._client._save_unlocked(store)
            return _LocalUserRecord(uid, email.strip(), {})

    def set_custom_user_claims(self, uid: str, claims: dict[str, Any]) -> None:
        with self._client._lock:
            store = self._client._load_unlocked()
            user = store.get('auth_users', {}).get(uid)
            if not user:
                raise LocalUserNotFoundError(f"User {uid} not found")
            user['custom_claims'] = dict(claims or {})
            self._client._save_unlocked(store)

    def get_user(self, uid: str) -> _LocalUserRecord:
        with self._client._lock:
            store = self._client._load_unlocked()
            user = store.get('auth_users', {}).get(uid)
            if not user:
                raise LocalUserNotFoundError(f"User {uid} not found")
            return _LocalUserRecord(uid, user['email'], user.get('custom_claims'))

    def delete_user(self, uid: str) -> None:
        with self._client._lock:
            store = self._client._load_unlocked()
            if uid not in store.get('auth_users', {}):
                raise LocalUserNotFoundError(f"User {uid} not found")
            store['auth_users'].pop(uid, None)
            self._client._save_unlocked(store)

    def sign_in_with_password(self, email: str, password: str) -> dict[str, Any]:
        normalized_email = (email or '').strip().lower()
        with self._client._lock:
            store = self._client._load_unlocked()
            for uid, record in store.get('auth_users', {}).items():
                if (record.get('email') or '').lower() == normalized_email:
                    if check_password_hash(record['password_hash'], password):
                        return {'localId': uid, 'email': record['email']}
                    raise LocalAuthError('INVALID_PASSWORD', 'Incorrect password')
            raise LocalAuthError('USER_NOT_FOUND', 'User not found')

    def update_password_by_email(self, email: str, new_password: str) -> bool:
        if len(new_password or '') < 6:
            raise ValueError("Password must be at least 6 characters")
        normalized_email = (email or '').strip().lower()
        with self._client._lock:
            store = self._client._load_unlocked()
            for record in store.get('auth_users', {}).values():
                if (record.get('email') or '').lower() == normalized_email:
                    record['password_hash'] = generate_password_hash(new_password)
                    self._client._save_unlocked(store)
                    return True
            return False

    def email_exists(self, email: str) -> bool:
        normalized_email = (email or '').strip().lower()
        with self._client._lock:
            store = self._client._load_unlocked()
            return any(
                (record.get('email') or '').lower() == normalized_email
                for record in store.get('auth_users', {}).values()
            )


def _is_delete_field_sentinel(value: Any) -> bool:
    return value is firestore.DELETE_FIELD or type(value).__name__ == 'Sentinel'


def _apply_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if _is_delete_field_sentinel(value):
            target.pop(key, None)
        else:
            target[key] = _serialize_value(copy.deepcopy(value))


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return {'__datetime__': value.isoformat()}
    if isinstance(value, datetime.date):
        return {'__date__': value.isoformat()}
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    return value


def _deserialize_value(value: Any) -> Any:
    if isinstance(value, dict):
        if '__datetime__' in value and len(value) == 1:
            return datetime.datetime.fromisoformat(value['__datetime__'])
        if '__date__' in value and len(value) == 1:
            return datetime.date.fromisoformat(value['__date__'])
        return {k: _deserialize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deserialize_value(v) for v in value]
    return value


def _matches_filters(data: dict[str, Any], filters: list[tuple[str, str, Any]]) -> bool:
    for field, op, expected in filters:
        actual = data.get(field)
        if op == '==':
            if actual != expected:
                return False
        elif op == '!=':
            if actual == expected:
                return False
        elif op == 'in':
            if actual not in (expected or []):
                return False
        else:
            return False
    return True


def create_backend(
    service_account_path: str = 'serviceAccountKey.json',
    storage_mode: str = 'auto',
    local_db_path: str = os.path.join('local_data', 'classroom_db.json'),
) -> tuple[Any, Any, str, str]:
    """Initialize either Firebase (Firestore + Auth) or local JSON persistence.

    Returns ``(db, auth_module, mode, reason)`` where ``mode`` is ``'firebase'``
    or ``'local'``.
    """
    mode = (storage_mode or 'auto').strip().lower()
    if mode == 'local':
        local_db = LocalFirestoreClient(local_db_path)
        return local_db, LocalAuth(local_db), 'local', 'Explicitly configured for local JSON storage'

    try:
        if not os.path.exists(service_account_path):
            raise FileNotFoundError(f"{service_account_path} not found")
        cred = credentials.Certificate(service_account_path)
        # Validate OAuth JWT exchange upfront so expired/revoked repo keys fail fast
        # rather than blocking Firestore requests for 60s retries.
        cred.get_credential().refresh(GoogleAuthRequest())
        try:
            get_app()
        except ValueError:
            initialize_app(cred)
        db = firestore.client()
        return db, fb_auth, 'firebase', 'Connected to live Firebase Auth & Firestore'
    except Exception as exc:
        if mode == 'firebase':
            raise
        logger.warning(
            "Firebase connection unavailable (%s). Falling back to local persistence at %s.",
            exc,
            local_db_path,
        )
        local_db = LocalFirestoreClient(local_db_path)
        return local_db, LocalAuth(local_db), 'local', f"Local fallback active ({exc})"
