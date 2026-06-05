"""Local image to base64 data URL converter."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "data" / "workspace"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

_MIME_MAP = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class AddImageToolLocalSchema(BaseModel):
    image_url: str


def _local_path_to_base64_data_url(image_url: str) -> str | None:
    try:
        path = Path(image_url).resolve()
    except (ValueError, OSError):
        return None

    if not str(path).startswith(str(_WORKSPACE_ROOT)):
        logger.warning("path traversal blocked: %s", image_url)
        return None
    if not path.exists():
        logger.warning("image not found: %s", path)
        return None
    if path.stat().st_size > _MAX_IMAGE_BYTES:
        logger.warning("image too large: %s", path)
        return None

    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    mime = _MIME_MAP.get(path.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64,{b64}"


class AddImageToolLocal(BaseTool):
    name: str = "Add image to content Local"
    description: str = "将本地图片转换为 base64 data URL。传入图片路径或 HTTP URL。"
    args_schema: type = AddImageToolLocalSchema

    def _run(self, image_url: str, **_) -> str:
        if image_url.startswith(("http://", "https://")):
            return image_url
        result = _local_path_to_base64_data_url(image_url)
        return result or image_url
