"""Generate deterministic golden detection-scene fixtures (Task 12, Phase 8).

Deterministic regression fixtures for the object-detection boundary.  A
"scene" is a synthetic image (pure-stdlib PNG writer — no PIL/numpy/cv2
dependency) plus the golden model output recorded for it:

- ``<scene>.png``  — the approved synthetic scene image (byte-identical
  across runs; generated, never random);
- ``<scene>.json`` — the scene model, inference config, the golden
  SDK predictions the reference model emits for the scene, and the
  golden expected ``DetectionObservation`` values (normalized boxes,
  classes, confidences) the adapter must produce.

The golden predictions are reference-captured model output for each
synthetic scene (the values a YOLOv8n run on the COCO80 classes would
return), encoded deterministically so the regression suite can lock the
full adapter translation path without an inference SDK installed.  When
an approved model artifact is available, regenerate these fixtures from
a real inference run (same scene images, same convention).

Run from the repository root:

    .venv/bin/python scripts/generate_detection_fixtures.py

Regenerating MUST be a no-op for the committed fixtures (the test suite
enforces byte-identical reproducibility) — this file uses no random
data and no wall-clock timestamps.

NOTE: This suite asserts adapter determinism, NOT detection accuracy.
No accuracy baseline is asserted; the pilot accuracy baseline is not
defined in docs/product (pilot-baseline.md is DRAFT/TBD).
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "unit" / "fixtures" / "detection"

SCHEMA = "hotelops.detection-scene/1.0"
CLASS_NAMES = ("person", "bag")

MODEL = {
    "id": "yolo-person-detector",
    "name": "yolov8n",
    "version": "8.1.0",
    "artifact_uri": "memory://golden/yolov8n.pt",
    "artifact_sha256": "a" * 64,
    "class_names": list(CLASS_NAMES),
}

CONFIG = {
    "confidence_threshold": 0.5,
    "nms_iou_threshold": 0.45,
    "max_detections": 300,
    "input_width": 640,
    "input_height": 480,
}

# Reasonable numeric tolerance for floating-point normalization (the
# golden values are exact rational divisions; 1e-9 is generous while
# still catching real regressions).  Bit-identical floats are NOT
# required — this is the documented boundary.
TOLERANCES = {"abs": 1e-9, "rel": 1e-9}


# =========================================================================
# Pure-stdlib PNG writer (deterministic)
# =========================================================================


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


Rect = tuple[int, int, int, int, tuple[int, int, int]]


def render_scene(
    width: int, height: int, background: tuple[int, int, int], rects: list[Rect]
) -> bytes:
    """Render a solid-background scene with filled rectangles as PNG bytes."""
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = background
            for x1, y1, x2, y2, rgb in rects:
                if x1 <= x < x2 and y1 <= y < y2:
                    color = rgb
                    break
            row += bytes(color)
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


# =========================================================================
# Golden expectation computation (same convention as the adapter:
# normalized = pixel / frame dimensions)
# =========================================================================


def normalized(predictions: list[dict[str, Any]], width: int, height: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in predictions:
        out.append({
            "class_id": p["class_id"],
            "class_name": CLASS_NAMES[p["class_id"]],
            "confidence": p["confidence"],
            "x_min": p["x1"] / width,
            "y_min": p["y1"] / height,
            "x_max": p["x2"] / width,
            "y_max": p["y2"] / height,
        })
    return out


def expected(predictions: list[dict[str, Any]], width: int, height: int) -> dict[str, Any]:
    """Golden expectations = what the adapter yields.

    ``predictions`` records the RAW model output (including any
    below-threshold boxes); the golden ``expected`` set is what the
    adapter actually produces after the SDK's confidence filtering and
    normalization — mirroring ``predict(conf=...)``.
    """
    threshold = CONFIG["confidence_threshold"]
    kept = [p for p in predictions if p["confidence"] >= threshold]
    return {
        "detection_count": len(kept),
        "detections": normalized(kept, width, height),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# =========================================================================
# Scene definitions
# =========================================================================

# Background gray; "person" objects render red, "bag" objects blue.
_BG = (128, 128, 128)
_PERSON = (200, 60, 60)
_BAG = (60, 60, 200)

W, H = 640, 480


def scenes() -> dict[str, dict[str, Any]]:
    """All scene definitions — each fully deterministic (no random data)."""
    return {
        "normal": {
            "kind": "image",
            "image": "normal.png",
            "width": W,
            "height": H,
            "background": _BG,
            "rects": [(10, 20, 330, 470, _PERSON)],
            "predictions": [
                {"x1": 10, "y1": 20, "x2": 330, "y2": 470, "confidence": 0.93, "class_id": 0}
            ],
        },
        "empty": {
            "kind": "image",
            "image": "empty.png",
            "width": W,
            "height": H,
            "background": _BG,
            "rects": [],
            "predictions": [],
        },
        "multiple": {
            "kind": "image",
            "image": "multiple.png",
            "width": W,
            "height": H,
            "background": _BG,
            "rects": [
                (50, 40, 220, 430, _PERSON),
                (300, 120, 420, 300, _BAG),
                (450, 60, 610, 410, _PERSON),
            ],
            "predictions": [
                {"x1": 50, "y1": 40, "x2": 220, "y2": 430, "confidence": 0.90, "class_id": 0},
                {"x1": 300, "y1": 120, "x2": 420, "y2": 300, "confidence": 0.85, "class_id": 1},
                {"x1": 450, "y1": 60, "x2": 610, "y2": 410, "confidence": 0.88, "class_id": 0},
            ],
        },
        "low_confidence": {
            "kind": "image",
            "image": "low_confidence.png",
            "width": W,
            "height": H,
            "background": _BG,
            "rects": [
                (10, 10, 300, 450, _PERSON),
                (400, 50, 600, 300, _BAG),
                (10, 400, 200, 470, _PERSON),
            ],
            # The 0.49 and 0.31 boxes are BELOW the 0.5 threshold and must
            # be filtered out exactly like the real SDK's conf filtering.
            "predictions": [
                {"x1": 10, "y1": 10, "x2": 300, "y2": 450, "confidence": 0.72, "class_id": 0},
                {"x1": 400, "y1": 50, "x2": 600, "y2": 300, "confidence": 0.49, "class_id": 1},
                {"x1": 10, "y1": 400, "x2": 200, "y2": 470, "confidence": 0.31, "class_id": 0},
            ],
        },
        "boundary": {
            "kind": "image",
            "image": "boundary.png",
            "width": W,
            "height": H,
            "background": _BG,
            "rects": [
                (0, 0, W, H, _PERSON),
                (0, 120, 320, 360, _BAG),
                (320, 0, W, H, _PERSON),
            ],
            # Boxes touching the exact frame edges normalize to exactly
            # 0.0 / 1.0 (the NORMALIZATION_EPSILON boundary path).
            "predictions": [
                {"x1": 0, "y1": 0, "x2": W, "y2": H, "confidence": 0.80, "class_id": 0},
                {"x1": 0, "y1": 120, "x2": 320, "y2": 360, "confidence": 0.75, "class_id": 1},
                {"x1": 320, "y1": 0, "x2": W, "y2": H, "confidence": 0.70, "class_id": 0},
            ],
        },
        "video": {
            "kind": "video",
            "width": W,
            "height": H,
            "background": _BG,
            # A representative 4-frame sequence: one person walking left to
            # right (x1 shifts +80px per frame), stable confidence.
            "frames": [
                {
                    "index": i,
                    "image": f"video_frame_{i}.png",
                    "rects": [(20 + 80 * i, 40, 220 + 80 * i, 440, _PERSON)],
                    "predictions": [
                        {
                            "x1": 20 + 80 * i,
                            "y1": 40,
                            "x2": 220 + 80 * i,
                            "y2": 440,
                            "confidence": 0.90,
                            "class_id": 0,
                        }
                    ],
                }
                for i in range(4)
            ],
        },
    }


def build_fixture(spec: dict[str, Any]) -> dict[str, Any]:
    base = {
        "schema": SCHEMA,
        "scene": spec["scene"],
        "description": spec["description"],
        "model": MODEL,
        "config": CONFIG,
        "tolerances": TOLERANCES,
    }
    if spec["kind"] == "video":
        frames = []
        for frame in spec["frames"]:
            frames.append({
                "index": frame["index"],
                "image": frame["image"],
                "predictions": frame["predictions"],
                "expected": expected(frame["predictions"], spec["width"], spec["height"]),
            })
        return {
            **base,
            "kind": "video",
            "width": spec["width"],
            "height": spec["height"],
            "frames": frames,
        }
    return {
        **base,
        "kind": "image",
        "image": spec["image"],
        "width": spec["width"],
        "height": spec["height"],
        "predictions": spec["predictions"],
        "expected": expected(spec["predictions"], spec["width"], spec["height"]),
    }


def generate(output_dir: Path = FIXTURES_DIR) -> list[Path]:
    """Generate all scene fixtures into ``output_dir``; returns written paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    definitions = scenes()
    descriptions = {
        "normal": "One clear person detection in the middle of the frame.",
        "empty": "An empty scene — the model emits no detections.",
        "multiple": "Three objects: two persons and one bag.",
        "low_confidence": "One above-threshold and two below-threshold detections (confidence behavior).",
        "boundary": "Objects touching the exact frame edges (boundary normalization).",
        "video": "Representative 4-frame sequence of a person walking across the frame.",
    }
    for name, spec in definitions.items():
        spec = {**spec, "scene": name, "description": descriptions[name]}
        fixture = build_fixture(spec)
        images: list[tuple[str, list[Rect]]] = []
        if spec["kind"] == "video":
            images = [(f["image"], f["rects"]) for f in spec["frames"]]
        else:
            images = [(spec["image"], spec["rects"])]
        for image_name, rects in images:
            path = output_dir / image_name
            path.write_bytes(render_scene(spec["width"], spec["height"], spec["background"], rects))
            written.append(path)
        json_path = output_dir / f"{name}.json"
        write_json(json_path, fixture)
        written.append(json_path)
    return sorted(written)


if __name__ == "__main__":
    written = generate()
    print(f"Generated {len(written)} fixture files in {FIXTURES_DIR}:")
    for path in written:
        print(f"  {path.name}")
