from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["pass", "inconclusive", "fail"]
SegmentStatus = Literal["normal", "exceeded", "unknown"]


class WindowCreate(BaseModel):
    start: datetime = Field(..., description="窗口起始时间（ISO 8601，naive 按 UTC）")
    end: datetime = Field(..., description="窗口结束时间")
    temp_min: float = Field(..., description="允许温度下限（含）")
    temp_max: float = Field(..., description="允许温度上限（含）")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="最长连续超限时长（秒，严格超过判不合格）")
    max_sampling_interval_seconds: float = Field(..., gt=0, description="最大采样间隔（秒，间隔严格超过记未知）")


class WindowOut(BaseModel):
    id: str
    start: datetime
    end: datetime
    temp_min: float
    temp_max: float
    max_continuous_exceed_seconds: float
    max_sampling_interval_seconds: float
    created_at: datetime


class ReadingSubmit(BaseModel):
    reading_id: str = Field(..., min_length=1, description="读数 ID")
    revision: int = Field(..., ge=1, description="修订号，从 1 起递增；更高修订号替换旧值")
    timestamp: datetime = Field(..., description="采样时间戳（ISO 8601）")
    temperature: float = Field(..., description="温度（℃），须为有限数值")


class ReadingOut(BaseModel):
    reading_id: str
    revision: int
    timestamp: datetime
    temperature: float
    superseded: bool = False


class ReadingSubmitResult(BaseModel):
    status: Literal["accepted", "replaced", "idempotent"]
    reading: ReadingOut
    replaced_revision: int | None = None


class SegmentOut(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float
    status: SegmentStatus
    temperatures: list[float] | None = None


class EvaluationOut(BaseModel):
    window_id: str
    verdict: Verdict
    segments: list[SegmentOut]
    normal_seconds: float
    exceeded_seconds: float
    unknown_seconds: float
    window_seconds: float
    longest_continuous_exceeded_seconds: float


class CertificateOut(BaseModel):
    id: str
    window_id: str
    version: int
    verdict: Verdict
    issued_at: datetime
    result: EvaluationOut
    readings: list[ReadingOut] = Field(..., description="签发时已提交读数的不可变快照")


class ErrorBody(BaseModel):
    error: dict
