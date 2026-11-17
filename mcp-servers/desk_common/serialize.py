"""numpy/pandas-safe JSON serialization for FastMCP tool results.

FastMCP's pydantic serializer crashes on numpy.bool_/int64/float64 and arrays,
which kills the stdio session. Pass ``tool_serializer`` to ``FastMCP(...)``.
"""
import json
import math


def json_native(obj):
    """Last-resort ``default=`` hook: numpy/pandas -> native, anything else -> str."""
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            pass
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def _clean_floats(value):
    """Replace NaN/inf (invalid JSON) with None, recursively."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {k: _clean_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_floats(v) for v in value]
    return value


def tool_serializer(result) -> str:
    try:
        return json.dumps(_clean_floats(result), default=json_native, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(result, default=json_native, ensure_ascii=False)
