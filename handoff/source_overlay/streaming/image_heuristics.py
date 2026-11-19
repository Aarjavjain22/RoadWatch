"""Cheap visibility signals derived straight from pixels (shared by the agent and the local UI)."""
import cv2
import numpy as np


def _leading(mask: np.ndarray) -> int:
    return len(mask) if mask.all() else int(np.argmax(~mask))


def _bar_length(row_mean: np.ndarray, row_std: np.ndarray, min_frac: float) -> int:
    """Length of a letterbox bar at the start of `row_mean`, or 0 if there is none.

    An editor's bar is pure black. Night sky is dark too, but noisy: on the Buffalo
    night clips it never stays this flat for more than 3.6% of the frame, so `min_frac`
    of pure-black rows marks a bar. What lies next to the bar is not a usable cue: it
    can be bright daylight or, under a bridge, almost as dark as the bar itself.
    """
    k = _leading((row_mean < 1) & (row_std < 1))
    if k == len(row_mean) or k < min_frac * len(row_mean):
        return 0
    # Compression smears the bar's edge: skip rows that are still nearly black, then two blended rows.
    k += _leading((row_mean[k:k + 8] < 4) & (row_std[k:k + 8] < 2))
    return k + 2


def crop_letterbox(image: np.ndarray, min_frac: float = 0.05) -> np.ndarray:
    """Remove black letterbox/pillarbox bars added by video editors, which otherwise drag
    brightness down (false "low_light") and hide most of the scene from the classifier."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    rows_m, rows_s = gray.mean(axis=1), gray.std(axis=1)
    cols_m, cols_s = gray.mean(axis=0), gray.std(axis=0)
    top = _bar_length(rows_m, rows_s, min_frac)
    bottom = _bar_length(rows_m[::-1], rows_s[::-1], min_frac)
    left = _bar_length(cols_m, cols_s, min_frac)
    right = _bar_length(cols_m[::-1], cols_s[::-1], min_frac)
    height, width = gray.shape
    if top + bottom >= height or left + right >= width:
        return image
    return image[top:height - bottom, left:width - right]


def compute_image_heuristics(image: np.ndarray) -> dict:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {
        "brightness": round(float(np.mean(gray)), 2),
        "contrast": round(float(np.std(gray)), 2),
        "blur_score": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2),
        "glare_ratio": round(float(np.mean(gray > 240)), 4),
    }
