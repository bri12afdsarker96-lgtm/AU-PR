"""帧栅格收口：把「每行时长」量化成「整数帧数」，并保证总帧数对齐 master。纯逻辑，可单测。

B 方案唯一必须做对的工程点：音频是整条叠的，所以画面总长必须 == master 总长，
否则片尾会露黑边（画面短）或音频拖尾（画面长）。做法：
    - 每行时长各自四舍五入到整数帧；
    - 末段吸收所有累计舍入残差，使 Σ帧数 == round(master 总时长 × fps)。
这样逐段用整数帧渲染、拼接后总帧数精确等于 master，收口到 ≤1 帧。
"""

from __future__ import annotations


def quantize_to_frames(
    durations: list[float],
    fps: float,
    total_seconds: float | None = None,
) -> list[int]:
    """把每行时长量化到整数帧；末段吸收残差，保证 Σ帧数 == round(total_seconds×fps)。

    total_seconds 为 master 音频实测总时长（收口基准）；缺省时用各行时长之和。
    每段至少 1 帧。返回与输入等长的整数帧数列表。
    """
    if fps <= 0:
        raise ValueError(f"fps 必须为正：{fps}")
    if not durations:
        return []

    if total_seconds is None:
        total_seconds = sum(durations)
    target_total = max(len(durations), round(total_seconds * fps))

    frames = [max(1, round(max(0.0, d) * fps)) for d in durations]
    residual = target_total - sum(frames)
    frames[-1] = max(1, frames[-1] + residual)

    # 若末段吸收后仍与目标有偏差（例如末段被 max(1,..) 抬高），再从前向后微调补齐。
    drift = target_total - sum(frames)
    index = 0
    while drift != 0 and index < len(frames):
        step = 1 if drift > 0 else -1
        if frames[index] + step >= 1:
            frames[index] += step
            drift -= step
        index += 1 if drift == 0 else 1
        if index >= len(frames) and drift != 0:
            # 兜底：全部落在末段（durations 极端时），保持不小于 1 帧。
            frames[-1] = max(1, frames[-1] + drift)
            drift = 0
    return frames


def frames_to_seconds(frames: int, fps: float) -> float:
    return round(frames / fps, 6)
