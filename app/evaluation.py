"""温度暴露审定引擎（纯函数，不依赖 Web / 数据库层）。

覆盖语义
--------
读数按时间戳升序排列。每条读数只覆盖从自身时间戳到下一条读数时间戳之间的
区段（该段温度取本段起点读数）：

* 相邻读数间隔严格大于 ``max_sampling_interval_seconds`` -> 整段记为未知；
* 窗口起点早于首条读数 -> 起点到首条读数为未知（边界无读数）；
* 末条读数到窗口终点：差值严格大于最大采样间隔 -> 未知，否则由末条读数覆盖；
* 窗口内没有任何读数 -> 整个窗口未知。

判定
----
* 最长连续超限时长严格大于允许值      -> ``fail``（不合格）
* 否则，只要存在未知区段              -> ``inconclusive``（待定）
* 其余                                -> ``pass``（合格）

温度恰好在允许区间边界上视为正常；间隔 / 连续超限时长“超过”一律按严格大于
比较（使用 EPS 容差消除浮点误差）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EPS = 1e-6  # 秒 / 摄氏度浮点比较容差

Status = Literal["normal", "exceeded", "unknown"]
Verdict = Literal["pass", "inconclusive", "fail"]


@dataclass(frozen=True)
class Reading:
    reading_id: str
    revision: int
    ts: float  # Unix 秒
    temperature: float


@dataclass(frozen=True)
class WindowSpec:
    start: float
    end: float
    temp_min: float
    temp_max: float
    max_continuous_exceed_seconds: float
    max_sampling_interval_seconds: float


@dataclass
class Segment:
    start: float
    end: float
    status: Status
    temperatures: list[float] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _round6(value: float) -> float:
    return round(float(value), 6)


def _classify(temperature: float, window: WindowSpec) -> Status:
    if window.temp_min - EPS <= temperature <= window.temp_max + EPS:
        return "normal"
    return "exceeded"


def _raw_segments(window: WindowSpec, readings: list[Reading]) -> list[Segment]:
    """按覆盖语义切出尚未合并的原始区段。"""
    segments: list[Segment] = []

    def add(start: float, end: float, status: Status, temperature: float | None = None) -> None:
        if end - start <= EPS:  # 零长度区段跳过
            return
        segments.append(
            Segment(start, end, status, [] if temperature is None else [temperature])
        )

    if not readings:
        add(window.start, window.end, "unknown")
        return segments

    ordered = sorted(readings, key=lambda r: r.ts)

    # 窗口起点 -> 首条读数：边界无读数，未知
    if ordered[0].ts - window.start > EPS:
        add(window.start, ordered[0].ts, "unknown")

    for current, nxt in zip(ordered, ordered[1:]):
        gap = nxt.ts - current.ts
        if gap > window.max_sampling_interval_seconds + EPS:
            add(current.ts, nxt.ts, "unknown")
        else:
            add(current.ts, nxt.ts, _classify(current.temperature, window), current.temperature)

    # 末条读数 -> 窗口终点
    last = ordered[-1]
    tail = window.end - last.ts
    if tail > window.max_sampling_interval_seconds + EPS:
        add(last.ts, window.end, "unknown")
    else:
        add(last.ts, window.end, _classify(last.temperature, window), last.temperature)

    return segments


def _merge_adjacent(segments: list[Segment]) -> list[Segment]:
    """合并时间相邻且状态相同的区段（未知会打断连续超限的累计）。"""
    merged: list[Segment] = []
    for seg in segments:
        if (
            merged
            and merged[-1].status == seg.status
            and abs(merged[-1].end - seg.start) <= EPS
        ):
            merged[-1].end = seg.end
            merged[-1].temperatures.extend(seg.temperatures)
        else:
            merged.append(
                Segment(seg.start, seg.end, seg.status, list(seg.temperatures))
            )
    return merged


def evaluate(window: WindowSpec, readings: list[Reading]) -> dict[str, Any]:
    """计算区段、分类时长、最长连续超限与结论。"""
    segments = _merge_adjacent(_raw_segments(window, readings))

    totals = {"normal": 0.0, "exceeded": 0.0, "unknown": 0.0}
    longest_exceeded = 0.0
    for seg in segments:
        totals[seg.status] += seg.duration
        if seg.status == "exceeded":
            longest_exceeded = max(longest_exceeded, seg.duration)

    # 判定：超限优先；无超限则有未知即待定；否则合格
    if longest_exceeded > window.max_continuous_exceed_seconds + EPS:
        verdict: Verdict = "fail"
    elif totals["unknown"] > EPS:
        verdict = "inconclusive"
    else:
        verdict = "pass"

    return {
        "segments": [
            {
                "start": _round6(seg.start),
                "end": _round6(seg.end),
                "duration_seconds": _round6(seg.duration),
                "status": seg.status,
                "temperatures": seg.temperatures if seg.status != "unknown" else None,
            }
            for seg in segments
        ],
        "normal_seconds": _round6(totals["normal"]),
        "exceeded_seconds": _round6(totals["exceeded"]),
        "unknown_seconds": _round6(totals["unknown"]),
        "window_seconds": _round6(window.end - window.start),
        "longest_continuous_exceeded_seconds": _round6(longest_exceeded),
        "verdict": verdict,
    }
