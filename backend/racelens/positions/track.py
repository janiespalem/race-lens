"""Track geometry exports from FastF1 telemetry.

build_track_outline: fastest-lap X/Y → normalized 600x400 outline + corner
markers (the .track.json consumed by the frontend map).
export_raw_positions: per-driver raw X/Y rows as JSONL for the Rust resampler.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
import json
import sys
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Any

# Telemetry kept before lights-out (t=0) so the formation lap / grid forming is
# visible on the map. ~3 min covers a formation lap; widen if a track needs more.
# A fixed window is more reliable than inferring formation-lap start from telemetry.
PRE_START_MS = 180_000

SESSION_NAME_MAP = {
    "R": "Race", "Q": "Qualifying",
    "FP1": "Practice 1", "FP2": "Practice 2", "FP3": "Practice 3",
}


def progress_path(
    relative_distance,
    xs,
    ys,
    *,
    extent: tuple[float, float, float, float],
    viewbox: tuple[float, float] = (600, 400),
    padding: float = 20,
    bins: int = 400,
) -> list[list[float]]:
    """Sample a lap by RelativeDistance so frontend progress follows its curves."""
    if bins < 2:
        raise ValueError("progress path requires at least two bins")
    samples = []
    for progress, x, y in zip(relative_distance, xs, ys):
        try:
            sample = float(progress), float(x), float(y)
        except (TypeError, ValueError):
            continue
        if all(isfinite(value) for value in sample):
            samples.append(sample)
    samples.sort()
    if len(samples) < 2:
        raise ValueError("progress path requires usable lap telemetry")
    distances = [sample[0] for sample in samples]
    x_min, y_min, x_max, y_max = extent
    width, height = viewbox
    available_width = width - 2 * padding
    available_height = height - 2 * padding
    scale = min(
        available_width / (x_max - x_min or 1),
        available_height / (y_max - y_min or 1),
    )
    offset_x = padding + (available_width - (x_max - x_min) * scale) / 2
    offset_y = padding + (available_height - (y_max - y_min) * scale) / 2
    points = []
    for index in range(bins):
        target = index / bins
        after = bisect_right(distances, target)
        if after == 0:
            x, y = samples[0][1:]
        elif after == len(samples):
            x, y = samples[-1][1:]
        else:
            before_progress, before_x, before_y = samples[after - 1]
            after_progress, after_x, after_y = samples[after]
            span = after_progress - before_progress
            ratio = (target - before_progress) / span if span else 0
            x = before_x + (after_x - before_x) * ratio
            y = before_y + (after_y - before_y) * ratio
        points.append([
            round(offset_x + (x - x_min) * scale, 1),
            round(height - (offset_y + (y - y_min) * scale), 1),
        ])
    return points


def sector_boundaries(
    sector_ends: Iterable[float],
    telemetry_time: Iterable[float],
    telemetry_progress: Iterable[float],
) -> list[dict[str, int | float]]:
    """Map timing-sector ends onto the same lap's progress, never equal thirds.

    Times are session seconds. Missing/out-of-range or non-monotonic source
    data yields no markers rather than extrapolated timing lines.
    """
    ends = list(sector_ends)
    times = list(telemetry_time)
    progress = list(telemetry_progress)
    if len(ends) != 2 or len(times) < 2 or len(times) != len(progress):
        return []
    if not all(isfinite(value) for value in ends + times + progress):
        return []
    if not times[0] < ends[0] < ends[1] < times[-1]:
        return []
    if any(b <= a for a, b in zip(times, times[1:])):
        return []
    if any(b < a for a, b in zip(progress, progress[1:])):
        return []
    markers = []
    for sector, end in enumerate(ends, 1):
        after = bisect_right(times, end)
        ratio = (end - times[after - 1]) / (times[after] - times[after - 1])
        distance = progress[after - 1] + ratio * (progress[after] - progress[after - 1])
        distance = round(float(distance), 6)
        if not 0 < distance < 1:
            return []
        markers.append({"sector": sector, "progress": distance})
    return markers if markers[0]["progress"] < markers[1]["progress"] else []


def _load_session(year: int, gp: str, session: str):
    """Load a FastF1 session with telemetry, using the local cache dir."""
    import fastf1

    import os

    cache_dir = Path(os.environ.get("FASTF1_CACHE", "fastf1_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))

    session_name = SESSION_NAME_MAP.get(session.upper(), session)
    print(f"Loading {year} {gp} {session_name} …", file=sys.stderr)
    ses = fastf1.get_session(year, gp, session_name)
    ses.load(telemetry=True)
    return ses


def _position_trace_is_clean(xs, ys) -> bool:
    points = [
        (float(x), float(y))
        for x, y in zip(xs, ys)
        if isfinite(float(x)) and isfinite(float(y))
    ]
    if len(points) < 100 or len(set(points)) / len(points) < 0.9:
        return False
    steps = [
        ((after[0] - before[0]) ** 2 + (after[1] - before[1]) ** 2) ** 0.5
        for before, after in zip(points, points[1:])
        if after != before
    ]
    return bool(steps) and max(steps) <= median(steps) * 10


def _geometry_lap(session):
    """Prefer the fastest lap whose position trace has no feed jumps or holds."""
    for _, lap in session.laps.sort_values("LapTime").iterlaps():
        positions = lap.get_pos_data()
        if positions is not None and _position_trace_is_clean(
            positions["X"], positions["Y"]
        ):
            return lap, positions
    lap = session.laps.pick_fastest()
    return lap, lap.get_pos_data()


def build_track_outline(year: int, gp: str, session: str, session_id: str) -> dict[str, Any]:
    """Fastest-lap X/Y → the .track.json payload (outline points + corners)."""
    ses = _load_session(year, gp, session)

    lap, pos = _geometry_lap(ses)

    xs = pos["X"].to_numpy()
    ys = pos["Y"].to_numpy()

    # Downsample to ~400 points uniformly
    n = len(xs)
    target = 400
    if n > target:
        step = n / target
        indices = [round(i * step) for i in range(target)]
        indices = [min(i, n - 1) for i in indices]
        xs = xs[indices]
        ys = ys[indices]

    # Normalize to viewBox 600x400 with padding 20, preserve aspect, invert Y
    VW, VH = 600, 400
    PAD = 20
    x_min, x_max = float(xs.min()), float(xs.max())
    y_min, y_max = float(ys.min()), float(ys.max())
    x_range = x_max - x_min or 1.0
    y_range = y_max - y_min or 1.0
    avail_w = VW - 2 * PAD
    avail_h = VH - 2 * PAD
    scale = min(avail_w / x_range, avail_h / y_range)
    # Center the smaller axis
    offset_x = PAD + (avail_w - x_range * scale) / 2
    offset_y = PAD + (avail_h - y_range * scale) / 2
    telemetry = lap.get_telemetry()
    import numpy as np

    telemetry_time = telemetry["SessionTime"].dt.total_seconds().to_numpy()
    telemetry_progress = telemetry["RelativeDistance"].to_numpy()
    usable = np.isfinite(telemetry_time) & np.isfinite(telemetry_progress)
    position_time = pos["SessionTime"].dt.total_seconds().to_numpy()
    relative_distance = np.interp(
        position_time,
        telemetry_time[usable],
        telemetry_progress[usable],
    )
    progress_points = progress_path(
        relative_distance, pos["X"], pos["Y"],
        extent=(x_min, y_min, x_max, y_max),
    )

    points = []
    for x, y in zip(xs, ys):
        nx = round(offset_x + (x - x_min) * scale, 1)
        # invert Y
        ny = round(VH - (offset_y + (y - y_min) * scale), 1)
        points.append([nx, ny])

    # Close contour: ensure last point == first
    if points and points[0] != points[-1]:
        points.append(points[0])

    # Corners (turn numbers) — same normalization as points
    corners = []
    try:
        circuit_info = ses.get_circuit_info()
        if circuit_info is not None and hasattr(circuit_info, "corners"):
            for _, row in circuit_info.corners.iterrows():
                cx = round(offset_x + (float(row["X"]) - x_min) * scale, 1)
                cy = round(VH - (offset_y + (float(row["Y"]) - y_min) * scale), 1)
                corners.append({"number": int(row["Number"]), "x": cx, "y": cy})
    except Exception:
        corners = []

    return {
        "session_id": session_id,
        "viewbox": [VW, VH],
        "extent_dm": [x_min, y_min, x_max, y_max],
        "padding": PAD,
        "points": points,
        "progress_points": progress_points,
        "sector_boundaries": sector_boundaries(
            [
                stamp.total_seconds() if hasattr(stamp, "total_seconds") else float("nan")
                for stamp in (lap.get("Sector1SessionTime"), lap.get("Sector2SessionTime"))
            ],
            telemetry_time,
            telemetry_progress,
        ),
        "corners": corners,
    }


def export_raw_positions(year: int, gp: str, session: str, out: Path) -> int:
    """Write per-driver raw X/Y telemetry rows as JSONL; return the row count.

    t=0 is anchored to the detected standing-start launch (fallback: lap-1
    start) and rows earlier than PRE_START_MS before it are dropped.
    """
    import pandas as pd

    ses = _load_session(year, gp, session)

    # session_zero for Date→session-time conversion
    session_zero = pd.Timestamp(ses.date) - pd.Timedelta(ses.session_start_time)

    # Anchor t=0 to the detected standing-start launch, in the SAME Date clock
    # used below, so the map's lights-out lines up with the cars actually
    # leaving the grid. Fall back to lap-1 start if no clean grid-hold is found.
    from racelens.positions.launch import detect_launch_ms
    t0_ms = detect_launch_ms(ses)
    if t0_ms is not None:
        print(f"launch detected → t0 = {t0_ms} ms", file=sys.stderr)
    else:
        from racelens.adapters._common import fastf1_lap1_start

        lap1 = ses.laps[ses.laps["LapNumber"] == 1]
        t0_td = fastf1_lap1_start(lap1)
        t0_ms = int(t0_td.total_seconds() * 1000) if not pd.isna(t0_td) else 0
        print("launch NOT detected — fell back to lap-1 start", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with out.open("w", encoding="utf-8") as fh:
        for drv_num in ses.pos_data:
            try:
                drv_abbr = ses.get_driver(str(drv_num))["Abbreviation"]
            except Exception:
                drv_abbr = str(drv_num)
            pos_df = ses.pos_data[drv_num]
            if pos_df is None or len(pos_df) == 0:
                continue
            for row in pos_df.itertuples():
                # Date column is absolute timestamp → session-relative ms → rebase
                try:
                    date_ts = pd.Timestamp(row.Date)
                    t_ms = int((date_ts - session_zero).total_seconds() * 1000) - t0_ms
                except Exception:
                    continue
                if t_ms < -PRE_START_MS:
                    continue
                try:
                    x = float(row.X)
                    y = float(row.Y)
                except Exception:
                    continue
                if not isfinite(x) or not isfinite(y):
                    continue
                line = json.dumps({"driver": drv_abbr, "t_ms": t_ms, "x": x, "y": y})
                fh.write(line + "\n")
                count += 1

    return count
