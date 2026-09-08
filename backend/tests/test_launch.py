from datetime import datetime, timedelta
import json
import sys
from types import SimpleNamespace

import pytest


def test_sector_boundaries_follow_source_times_not_equal_track_thirds():
    from racelens.positions.track import sector_boundaries

    assert sector_boundaries(
        [125, 170], [100, 150, 200], [0, 0.4, 1],
    ) == [{"sector": 1, "progress": 0.2}, {"sector": 2, "progress": 0.64}]


@pytest.mark.parametrize("ends,times,progress", [
    ([float("nan"), 170], [100, 150, 200], [0, 0.4, 1]),
    ([90, 170], [100, 150, 200], [0, 0.4, 1]),
    ([125, 210], [100, 150, 200], [0, 0.4, 1]),
    ([170, 125], [100, 150, 200], [0, 0.4, 1]),
    ([125, 170], [100, 100, 200], [0, 0.4, 1]),
    ([125, 170], [100, 150, 200], [0, 0.4, float("inf")]),
    ([125, 170], [100, 150, 200], [0, 0.4, 0.3]),
])
def test_sector_boundaries_reject_missing_or_unusable_source(ends, times, progress):
    from racelens.positions.track import sector_boundaries

    assert sector_boundaries(ends, times, progress) == []


def test_progress_path_is_indexed_by_relative_distance():
    from racelens.positions.track import progress_path

    assert progress_path(
        [0, 0.25, 0.5, 0.75],
        [0, 10, 10, 0],
        [0, 0, 10, 10],
        extent=(0, 0, 10, 10),
        viewbox=(100, 100),
        padding=0,
        bins=4,
    ) == [[0, 100], [100, 100], [100, 0], [0, 0]]


def test_track_geometry_rejects_position_feed_jumps():
    from racelens.positions.track import _position_trace_is_clean

    smooth = list(range(120))
    jumped = smooth.copy()
    jumped[60] = 10_000

    assert _position_trace_is_clean(smooth, smooth)
    assert not _position_trace_is_clean(jumped, smooth)


def test_detect_launch_ms_uses_session_clock(monkeypatch):
    from racelens.positions import launch

    class Session:
        date = datetime(2021, 3, 28, 14, 0)
        session_start_time = timedelta(minutes=90)

    monkeypatch.setattr(
        launch,
        "detect_launch_date",
        lambda _: datetime(2021, 3, 28, 13, 15, 1),
    )

    assert launch.detect_launch_ms(Session()) == 2_701_000


def test_launch_detection_uses_first_start_before_a_red_flag_restart():
    from racelens.positions.launch import _first_launch_index

    speed = [0] * 8 + [10] * 20 + [0] * 8 + [10] * 20
    spread = [5] * 8 + list(range(10, 210, 10)) + [5] * 8 + list(range(10, 210, 10))

    assert _first_launch_index(speed, spread, stop=1, move=4) == 8


def test_raw_positions_skip_non_finite_coordinates(tmp_path, monkeypatch):
    from racelens.positions import launch, track

    session_zero = datetime(2026, 7, 28, 12)

    class Rows(list):
        def itertuples(self):
            return iter(self)

    class Session:
        date = session_zero
        session_start_time = timedelta(0)
        pos_data = {
            "1": Rows([
                SimpleNamespace(Date=session_zero, X=1.0, Y=2.0),
                SimpleNamespace(Date=session_zero + timedelta(seconds=1), X=float("nan"), Y=2.0),
                SimpleNamespace(Date=session_zero + timedelta(seconds=2), X=float("inf"), Y=2.0),
                SimpleNamespace(Date=session_zero + timedelta(seconds=3), X=3.0, Y=float("-inf")),
            ]),
        }

        @staticmethod
        def get_driver(_):
            return {"Abbreviation": "VER"}

    monkeypatch.setattr(track, "_load_session", lambda *_: Session())
    monkeypatch.setattr(launch, "detect_launch_ms", lambda _: 0)
    monkeypatch.setitem(
        sys.modules,
        "pandas",
        SimpleNamespace(Timestamp=lambda value: value, Timedelta=lambda value: value),
    )
    output = tmp_path / "positions.jsonl"

    assert track.export_raw_positions(2026, "Test", "R", output) == 1
    assert [json.loads(line) for line in output.read_text().splitlines()] == [{
        "driver": "VER", "t_ms": 0, "x": 1.0, "y": 2.0,
    }]
