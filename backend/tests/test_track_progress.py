from types import SimpleNamespace

from racelens.positions.track_progress import _progress_telemetry


def test_progress_keeps_padding_distance_and_position_grid_without_driver_ahead():
    calls = []
    car = SimpleNamespace()
    car.add_distance = lambda: (calls.append("distance") or car)
    car.add_relative_distance = lambda: (calls.append("relative") or car)
    position = SimpleNamespace()

    def merge(other):
        assert other is car
        calls.append("merge")
        return position

    position.merge_channels = merge
    lap = SimpleNamespace()
    lap.get_pos_data = lambda **kwargs: (calls.append(kwargs) or position)
    lap.get_car_data = lambda **kwargs: (calls.append(kwargs) or car)

    def slice_by_lap(actual, *, interpolate_edges):
        assert actual is lap and interpolate_edges
        return "progress telemetry"

    position.slice_by_lap = slice_by_lap
    # No get_telemetry/add_driver_ahead exists on these fakes: neither is needed.
    assert _progress_telemetry(lap) == "progress telemetry"
    assert calls == [
        {"pad": 1, "pad_side": "both"}, {"pad": 1, "pad_side": "both"},
        "distance", "relative", "merge",
    ]
