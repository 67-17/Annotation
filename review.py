"""Deterministic curve review; candidates always require human approval."""
import numpy as np


def refine(curves, side, start, end, threshold, release_threshold, min_samples=2):
    if threshold <= release_threshold or release_threshold < 0:
        raise ValueError("接触阈值必须大于释放阈值，且释放阈值不能为负")
    points = np.asarray(curves, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
        raise ValueError("曲线包含无效数值")
    points = points[(points[:, 0] >= start) & (points[:, 0] <= end)]
    if len(points) < min_samples:
        raise ValueError("选定范围内采样点不足")
    active = False
    run = 0
    begin = None
    intervals = []
    for i, (t, value) in enumerate(points):
        changed = value <= release_threshold if active else value >= threshold
        run = run + 1 if changed else 0
        if run >= min_samples:
            boundary = float(points[i - min_samples + 1, 0])
            if active:
                intervals.append({"side": side, "start": begin, "end": boundary})
                begin = None
            else:
                begin = boundary
            active = not active
            run = 0
    if active:
        intervals.append({"side": side, "start": begin, "end": None})
    return {"intervals": intervals, "window_start": start,
            "warning": "范围起点前的接触状态未知；边界均为待确认候选"}
