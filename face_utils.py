"""Face detection, encoding, and comparison utilities.

Uses ``face_recognition`` (dlib 128D embeddings) when installed, and
automatically falls back to OpenCV Haar-cascade detection with a 128-element
illumination-normalized spatial + gradient descriptor so the application runs
cleanly in environments without CMake/C++ build tools.
"""

from __future__ import annotations

import base64
import os
from typing import Iterable

import cv2
import numpy as np

try:
    import face_recognition  # type: ignore

    HAS_FACE_RECOGNITION = True
except ImportError:
    face_recognition = None
    HAS_FACE_RECOGNITION = False

_CASCADE_DATA = getattr(cv2, 'data', None)
_CASCADE_CLS = getattr(cv2, 'CascadeClassifier', None)
_FACE_CASCADE = None
if _CASCADE_DATA is not None and _CASCADE_CLS is not None and hasattr(_CASCADE_DATA, 'haarcascades'):
    cascade_file = f"{_CASCADE_DATA.haarcascades}haarcascade_frontalface_default.xml"
    if os.path.exists(cascade_file):
        loaded = _CASCADE_CLS(cascade_file)
        if not loaded.empty():
            _FACE_CASCADE = loaded


def decode_base64_image(image_base64: str) -> np.ndarray:
    """Decode a base64 or data-URI image string into an OpenCV BGR array."""
    if not image_base64 or not isinstance(image_base64, str):
        raise ValueError("Invalid image data")
    payload = image_base64.split(',', 1)[1] if ',' in image_base64 else image_base64
    try:
        raw_bytes = base64.b64decode(payload)
    except Exception as exc:
        raise ValueError("Invalid image data") from exc
    if not raw_bytes:
        raise ValueError("Invalid image data")
    nparr = np.frombuffer(raw_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Invalid image data")
    return img


def _opencv_face_encoding(bgr_img: np.ndarray) -> np.ndarray:
    """Detect a single face with OpenCV and compute a 128D normalized descriptor."""
    if _FACE_CASCADE is None:
        raise RuntimeError("OpenCV Haar cascade classifier unavailable")
    gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    equalized = clahe.apply(gray)

    faces = _FACE_CASCADE.detectMultiScale(
        equalized,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(40, 40),
    )
    if len(faces) == 0:
        raise ValueError("No face detected. Try again.")
    if len(faces) > 1:
        raise ValueError("Multiple faces detected. Please capture only one face.")

    x, y, w, h = faces[0]
    crop = equalized[y : y + h, x : x + w]

    # 8x8 normalized intensity patch (64 dims) + 4x4 gradient orientation stats (64 dims)
    patch_small = cv2.resize(crop, (8, 8), interpolation=cv2.INTER_AREA).astype(np.float32).flatten()
    patch_std = float(np.std(patch_small))
    if patch_std > 1e-6:
        patch_small = (patch_small - float(np.mean(patch_small))) / patch_std
    else:
        patch_small = np.zeros(64, dtype=np.float32)

    patch_grad = cv2.resize(crop, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    gx = cv2.Sobel(patch_grad, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(patch_grad, cv2.CV_32F, 0, 1, ksize=3)
    grad_features = []
    for row in range(0, 32, 8):
        for col in range(0, 32, 8):
            cell_gx = gx[row : row + 8, col : col + 8]
            cell_gy = gy[row : row + 8, col : col + 8]
            grad_features.extend([
                float(np.mean(np.abs(cell_gx))),
                float(np.mean(np.abs(cell_gy))),
                float(np.mean(cell_gx)),
                float(np.mean(cell_gy)),
            ])
    grad_vec = np.asarray(grad_features, dtype=np.float32)
    grad_norm = float(np.linalg.norm(grad_vec))
    if grad_norm > 1e-6:
        grad_vec = grad_vec / grad_norm

    descriptor = np.concatenate([patch_small, grad_vec]).astype(np.float32)
    total_norm = float(np.linalg.norm(descriptor))
    if total_norm > 1e-6:
        descriptor = descriptor / total_norm
    return descriptor


def get_face_encoding(image_base64: str) -> np.ndarray:
    """Return a 128-element face encoding vector from a base64-encoded image."""
    img = decode_base64_image(image_base64)
    if HAS_FACE_RECOGNITION:
        rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb_img)
        if len(locations) == 0:
            raise ValueError("No face detected. Try again.")
        if len(locations) > 1:
            raise ValueError("Multiple faces detected. Please capture only one face.")
        encodings = face_recognition.face_encodings(rgb_img, locations)
        return np.asarray(encodings[0], dtype=np.float32)
    return _opencv_face_encoding(img)


def match_face_encoding(
    known_encodings: Iterable[np.ndarray],
    candidate_encoding: np.ndarray,
    tolerance: float | None = None,
) -> tuple[int | None, float | None]:
    """Return ``(best_index, distance)`` for the closest matching face or ``(None, best_dist)``."""
    candidate = np.asarray(candidate_encoding, dtype=np.float32).flatten()
    known_list = [np.asarray(enc, dtype=np.float32).flatten() for enc in known_encodings]
    if not known_list:
        return None, None

    if tolerance is None:
        tolerance = 0.5 if HAS_FACE_RECOGNITION else 0.55

    if HAS_FACE_RECOGNITION and all(len(k) == len(candidate) == 128 for k in known_list):
        distances = face_recognition.face_distance(known_list, candidate)
    else:
        distances = np.array([
            float(np.linalg.norm(k - candidate)) if len(k) == len(candidate) else float('inf')
            for k in known_list
        ])

    best_idx = int(np.argmin(distances))
    best_dist = float(distances[best_idx])
    if best_dist <= tolerance:
        return best_idx, best_dist
    return None, best_dist
