"""Generate the deterministic vertical-slice fixture (Task 18.2).

A synthetic, fully deterministic "video" — a sequence of pure-stdlib PNG
frames — that drives the ENTIRE vertical slice without a live camera and
without any external dependency:

    Frame 0..N:      empty scene (no person)
    Frame N..N+X:    person box walks into the ROI and stays inside
    Frame N+X..M:    person remains inside (occupancy sustained)
    Frame M:         person exits (scene empty again)

Each frame records, in the golden manifest:

- pixel dimensions (width, height) and the deterministic frame index;
- the golden detection (class=person, deterministic pixel box) the
  reference detector emits for the scene — encoded so the adapter path
  can be locked WITHOUT an inference SDK installed;
- the ROI the person is inside of (deterministic zone polygon);
- the expected presence transition (enter) and the expected occupancy
  transition + event per the Task 15 presence/occupancy FSM semantics.

The frames are generated, never random: the same input produces
byte-identical PNGs and a byte-identical manifest on every run
(reproducibility is asserted by the test suite). FPS and timestamps are
fixed constants carried in the manifest — event-time is derived
deterministically as ``CAPTURE_TIME + frame_index / FPS`` (never wall
clock).

Run from the repository root:

    .venv/bin/python scripts/generate_vertical_slice_fixture.py

Regenerating MUST be a no-op for the committed fixtures.
"""

from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

FIXTURES_DIR = (
    Path(__file__).resolve().parents[1] / "tests" / "unit" / "fixtures" / "vertical_slice"
)

SCHEMA = "hotelops.vertical-slice/1.0"

# --- Deterministic scene geometry (fixed constants, never random) ---------
WIDTH = 320
HEIGHT = 240
FPS = 10.0
FRAME_COUNT = 30
# Person box at frame 6 (entry), 7..25 inside ROI, exit complete by 28.
ENTER_FRAME = 6
# Box center first strictly inside the ROI polygon at frame 7
# (frame 6 the center sits exactly on the left edge).
INSIDE_ROI_FROM = 7
EXIT_FRAME = 27
EMPTY_FROM = 28
CAPTURE_TIME = "2026-08-01T10:00:00+00:00"

# ROI: a deterministic quadrilateral zone (the front-desk area). The
# person box's center is inside it from INSIDE_ROI_FROM onward.
ROI_POLYGON: list[tuple[float, float]] = [(40, 40), (280, 40), (280, 200), (40, 200)]
ROI: dict[str, Any] = {
    "id": "zone-lobby",
    "kind": "zone",
    "polygon": ROI_POLYGON,
}

# Person box trajectory: walks left → right across the ROI.
BOX_W = 40
BOX_H = 80
BOX_Y = 80  # top of the box; center y = 120 (inside ROI y-range)
BOX_X_START = 20  # frame 6: x=20, center x=40 (on ROI left edge)
BOX_X_PER_FRAME = 8


def box_x(frame: int) -> int:
    """Deterministic person-box left edge for a frame (linear walk)."""
    if frame < ENTER_FRAME:
        return -1000  # off-frame → no detection
    return BOX_X_START + (frame - ENTER_FRAME) * BOX_X_PER_FRAME


def person_present(frame: int) -> bool:
    return ENTER_FRAME <= frame < EMPTY_FROM


def person_inside_roi(frame: int) -> bool:
    """Box center inside the ROI polygon (deterministic point-in-rect)."""
    if not person_present(frame):
        return False
    cx = box_x(frame) + BOX_W / 2
    cy = BOX_Y + BOX_H / 2
    xs = [p[0] for p in ROI_POLYGON]
    ys = [p[1] for p in ROI_POLYGON]
    return min(xs) < cx < max(xs) and min(ys) < cy < max(ys)


# =============================================================================
# Pure-stdlib PNG writer (deterministic; same convention as the detection
# fixture generator)
# =============================================================================


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def render_frame(
    width: int,
    height: int,
    *,
    person: bool,
    person_x: int,
    background: tuple[int, int, int] = (0, 0, 0),
    person_color: tuple[int, int, int] = (200, 200, 200),
) -> bytes:
    """Render one deterministic frame as PNG bytes (no PIL/numpy/cv2).

    A solid background plus an optional filled person rectangle. Every
    pixel is a deterministic function of the frame parameters.
    """
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = background
            if person and person_x <= x < person_x + BOX_W and BOX_Y <= y < BOX_Y + BOX_H:
                color = person_color
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


# =============================================================================
# Golden manifest
# =============================================================================


# Fixed identity constants for the spatial/configuration layer: the
# controlled venue, camera, and published configuration version the
# fixture's ROI is published under (Task 10). Deterministic UUIDs.
TENANT_ID = "11111111-1111-4111-8111-111111111111"
VENUE_ID = "22222222-2222-4222-8222-222222222222"
CAMERA_ID = "33333333-3333-4333-8333-333333333333"
CONFIGURATION_VERSION_ID = "44444444-4444-4444-8444-444444444444"

# The spatial engine classifies points in the coordinate space of the
# geometry. This controlled fixture declares its ROI polygon in
# VENUE_LOCAL with a deterministic 1:1 venue mapping (venue-local
# coordinates == fixture pixel coordinates); no camera calibration is
# needed because the fixture is its own venue plane.
ZONE_PROFILE_ID = "zone-lobby"
CAMERA_PROFILE_ID = "cam-lobby"


def spatial_status(frame: int) -> str | None:
    """The golden spatial outcome for a frame's track centroid.

    The centroid is ``(box_x + BOX_W / 2, BOX_Y + BOX_H / 2)``. Frame 6
    places the centroid exactly on the ROI's left edge (x == 40) — the
    documented BOUNDARY case the engine refuses to silently convert
    (``BoundaryPolicyUndefinedError``). Frames 7..27 are strictly inside.
    Frames with no person produce no spatial evaluation (None).
    """
    if not person_present(frame):
        return None
    if person_inside_roi(frame):
        return "inside"
    return "boundary"


# The golden track identity: ONE persistent person track across the
# whole on-frame interval (frames 6..27). Deterministic — derived from
# the fixed scene constants, never from the tracker's internal RNG.
GOLDEN_TRACK_ID = "track-person-001"


def golden_tracks(frame: int) -> list[dict[str, Any]]:
    """The golden tracker output for a frame (one stable track).

    The track is present exactly while the person box is on-frame and
    keeps the SAME identity across every frame (no re-association, no
    fragmentation) — a short occlusion inside the interval must never
    split it.
    """
    if not person_present(frame):
        return []
    return [
        {
            "track_id": GOLDEN_TRACK_ID,
            "class_name": "person",
            "state": "active",
            # box center — the deterministic spatial anchor
            "center_x": box_x(frame) + BOX_W / 2,
            "center_y": BOX_Y + BOX_H / 2,
        }
    ]


def golden_detection(frame: int) -> list[dict[str, Any]]:
    """The golden detector output for a frame (pixel box, class person).

    Deterministic: a person is present exactly when the box is on-frame.
    The box is a plain rectangle — the golden box equals the rendered
    box (this fixture locks the DETECTOR BOUNDARY translation, not
    detection accuracy).
    """
    if not person_present(frame):
        return []
    x = box_x(frame)
    return [
        {
            "class_id": 0,
            "class_name": "person",
            "confidence": 0.95,
            # pixel box (matches the rendered rectangle)
            "x1": x,
            "y1": BOX_Y,
            "x2": x + BOX_W,
            "y2": BOX_Y + BOX_H,
        }
    ]


def spatial_point(frame: int) -> dict[str, Any] | None:
    """The golden centroid point for a frame (venue-local, box center).

    ``person_inside_roi`` uses the same centroid arithmetic, so the
    point and the expected status can never drift apart.
    """
    if not person_present(frame):
        return None
    return {
        "x": box_x(frame) + BOX_W / 2,
        "y": BOX_Y + BOX_H / 2,
        "coordinate_space": "venue_local",
        "policy": "centroid",
    }


def expected_timeline() -> list[dict[str, Any]]:
    """The golden per-frame timeline (deterministic, full slice)."""
    timeline: list[dict[str, Any]] = []
    capture = datetime.fromisoformat(CAPTURE_TIME)
    for frame in range(FRAME_COUNT):
        present = person_present(frame)
        inside = person_inside_roi(frame)
        # Presence FSM: ENTERING from first observation; ENTER_CONFIRMED
        # at the entry-confirmation frame (presence entry_confirmation).
        # For this fixture the confirmation is deterministic: entry is
        # confirmed once the person is continuously observed for the
        # entry confirmation duration (2 frames).
        enter_confirmed = present and frame == ENTER_FRAME + 1
        timeline.append({
            "frame_index": frame,
            "event_time": (capture + timedelta(seconds=frame / FPS)).isoformat(),
            "person_present": present,
            "person_inside_roi": inside,
            "golden_detections": golden_detection(frame),
            "golden_tracks": golden_tracks(frame),
            "presence": "enter_confirmed"
            if enter_confirmed
            else ("present" if present else "absent"),
            "occupancy_transition": (
                "enter_confirmed"
                if enter_confirmed
                else ("exit_confirmed" if frame == EXIT_FRAME + 1 else None)
            ),
            # Task 18.6 spatial slice: the golden spatial outcome + the
            # centroid point the Task 14 engine evaluates (same arithmetic
            # as person_inside_roi, so they can never drift apart).
            "spatial_status": spatial_status(frame),
            "spatial_point": spatial_point(frame),
        })
    return timeline


def main() -> None:
    """Generate the fixture frames + golden manifest (idempotent)."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for frame in range(FRAME_COUNT):
        present = person_present(frame)
        png = render_frame(
            WIDTH,
            HEIGHT,
            person=present,
            person_x=box_x(frame) if present else 0,
        )
        (FIXTURES_DIR / f"frame_{frame:03d}.png").write_bytes(png)

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "description": (
            "Deterministic vertical-slice fixture: person enters ROI, remains "
            "inside, occupancy threshold satisfied, one occupancy event."
        ),
        "metadata": {
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS,
            "frame_count": FRAME_COUNT,
            "capture_time": CAPTURE_TIME,
            "source_type": "recorded",
            "deterministic": True,
            "no_network": True,
        },
        "roi": ROI,
        # Task 10 published configuration for the spatial slice: the ONE
        # published version the ROI is evaluated under. The zone polygon
        # is the ROI in VENUE_LOCAL (1:1 fixture-pixel mapping).
        "spatial": {
            "tenant_id": TENANT_ID,
            "venue_id": VENUE_ID,
            "camera_id": CAMERA_ID,
            "camera_profile_id": CAMERA_PROFILE_ID,
            "configuration_version_id": CONFIGURATION_VERSION_ID,
            "zone_profile_id": ZONE_PROFILE_ID,
            "zone_geometry": {
                "geometry_id": "g-zone-lobby",
                "geometry_type": "polygon",
                "coordinate_space": "venue_local",
                "geometry_scope": "venue",
                "coordinates": ROI["polygon"],
            },
            "point_policy": "centroid",
        },
        # The governed detector model (Task 12) whose golden predictions
        # are the per-frame ``golden_detections`` in the timeline.
        "model": {
            "id": "yolo-person-detector",
            "name": "yolov8n",
            "version": "8.1.0",
            "artifact_uri": "memory://vertical-slice/yolov8n.pt",
            "artifact_sha256": "a" * 64,
            "class_names": ["person"],
        },
        # The detector configuration the slice is evaluated under
        # (confidence threshold, NMS policy, bounded max detections).
        "config": {
            "confidence_threshold": 0.5,
            "nms_iou_threshold": 0.45,
            "max_detections": 300,
        },
        "trajectory": {
            "box_width": BOX_W,
            "box_height": BOX_H,
            "box_y": BOX_Y,
            "enter_frame": ENTER_FRAME,
            "inside_roi_from": INSIDE_ROI_FROM,
            "exit_frame": EXIT_FRAME,
            "empty_from": EMPTY_FROM,
        },
        "expected_events": [
            {
                "event_type": "operational.occupancy_session",
                "phase": "started",
                "trigger_frame": ENTER_FRAME + 1,
                "count": 1,
            }
        ],
        "timeline": expected_timeline(),
    }
    (FIXTURES_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {FRAME_COUNT} frames + manifest.json to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
