from app.evaluation import Reading as R
from app.evaluation import WindowSpec, evaluate

W = WindowSpec(
    start=0.0,
    end=1000.0,
    temp_min=18.0,
    temp_max=22.0,
    max_continuous_exceed_seconds=250.0,
    max_sampling_interval_seconds=100.0,
)


def seg_statuses(result):
    return [(s["status"], s["duration_seconds"]) for s in result["segments"]]


def test_no_readings_entire_window_unknown():
    result = evaluate(W, [])
    assert result["verdict"] == "inconclusive"
    assert seg_statuses(result) == [("unknown", 1000.0)]


def test_all_normal_pass_and_intervals_equal_limit_are_covered():
    readings = [R("a", 1, 0.0, 20.0), R("b", 1, 100.0, 20.0), R("c", 1, 200.0, 20.0)]
    w = WindowSpec(0, 200, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert result["verdict"] == "pass"
    # 相邻同状态区段被合并
    assert seg_statuses(result) == [("normal", 200.0)]
    assert result["longest_continuous_exceeded_seconds"] == 0.0


def test_boundary_temperatures_are_normal():
    readings = [R("a", 1, 0.0, 18.0), R("b", 1, 100.0, 22.0), R("c", 1, 200.0, 20.0)]
    w = WindowSpec(0, 300, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert result["verdict"] == "pass"
    assert result["normal_seconds"] == 300.0


def test_long_exceeded_run_fails():
    # 0..300 全程 23℃，末段 100s 恰为最大间隔仍由末读数覆盖，连续超限 300s > 250s
    readings = [R("a", 1, 0.0, 23.0), R("b", 1, 100.0, 23.0), R("c", 1, 200.0, 23.0)]
    w = WindowSpec(0, 300, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert result["verdict"] == "fail"
    assert result["longest_continuous_exceeded_seconds"] == 300.0
    assert result["exceeded_seconds"] == 300.0


def test_short_exceeded_at_limit_passes():
    # 连续超限 250s == 限值（不严格超过），且无未知 -> 合格
    readings = [R("a", 1, 0.0, 23.0), R("b", 1, 100.0, 23.0),
                R("c", 1, 200.0, 23.0), R("d", 1, 250.0, 20.0)]
    w = WindowSpec(0, 350, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert result["verdict"] == "pass"
    assert result["longest_continuous_exceeded_seconds"] == 250.0


def test_two_exceeded_runs_separated_by_normal_not_merged():
    readings = [
        R("a", 1, 0.0, 23.0),    # [0,100) 超限
        R("b", 1, 100.0, 20.0),  # [100,200) 正常
        R("c", 1, 200.0, 23.0),  # [200,300) 超限
        R("d", 1, 300.0, 20.0),
    ]
    w = WindowSpec(0, 400, 18, 22, 250, 100)
    result = evaluate(w, readings)
    statuses = [s["status"] for s in result["segments"]]
    assert statuses == ["exceeded", "normal", "exceeded", "normal"]
    assert result["longest_continuous_exceeded_seconds"] == 100.0
    assert result["verdict"] == "pass"


def test_leading_boundary_without_reading_is_unknown():
    readings = [R("a", 1, 200.0, 20.0), R("b", 1, 300.0, 20.0)]
    w = WindowSpec(0, 400, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert result["verdict"] == "inconclusive"
    assert seg_statuses(result)[0] == ("unknown", 200.0)
    assert result["unknown_seconds"] == 200.0


def test_gap_over_interval_is_unknown_and_breaks_exceeded_run():
    # 间隔 600 > 100 -> 未知；未知前后的超限不连续，100s 未超 250s 限值 -> 待定
    readings = [R("a", 1, 0.0, 23.0), R("b", 1, 600.0, 23.0), R("c", 1, 700.0, 20.0)]
    w = WindowSpec(0, 700, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert seg_statuses(result) == [("unknown", 600.0), ("exceeded", 100.0)]
    assert result["longest_continuous_exceeded_seconds"] == 100.0
    assert result["verdict"] == "inconclusive"


def test_tail_over_interval_is_unknown():
    readings = [R("a", 1, 0.0, 20.0), R("b", 1, 100.0, 20.0)]
    w = WindowSpec(0, 400, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert seg_statuses(result) == [("normal", 100.0), ("unknown", 300.0)]
    assert result["verdict"] == "inconclusive"


def test_unknown_with_over_limit_exceeded_prefers_fail():
    # 超限连续 300s > 250s，同时存在未知区段：超限优先 -> 不合格
    readings = [
        R("a", 1, 0.0, 23.0), R("b", 1, 100.0, 23.0),
        R("c", 1, 200.0, 23.0), R("d", 1, 300.0, 23.0),
        R("e", 1, 600.0, 20.0),
    ]
    w = WindowSpec(0, 700, 18, 22, 250, 100)
    result = evaluate(w, readings)
    assert result["longest_continuous_exceeded_seconds"] == 300.0
    assert result["unknown_seconds"] == 300.0
    assert result["verdict"] == "fail"
