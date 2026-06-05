"""Token counting with 3-level fallback."""

from __future__ import annotations

import json
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_tokenizer = None
_mode = "rough"


@lru_cache(maxsize=1)
def _init_tokenizer() -> str:
    global _tokenizer, _mode
    try:
        import dashscope  # noqa: F401
        from dashscope import get_tokenizer
        _tokenizer = get_tokenizer("qwen-max")
        _mode = "qwen_official"
        logger.info("token_counter: using qwen_official tokenizer")
        return _mode
    except Exception:
        pass

    try:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(
            "Qwen/Qwen2-7B-Instruct", local_files_only=True
        )
        _mode = "hf_qwen"
        logger.info("token_counter: using hf_qwen tokenizer")
        return _mode
    except Exception:
        pass

    _mode = "rough"
    logger.info("token_counter: using rough estimation (len // 2)")
    return _mode


def count_tokens(messages: list[dict]) -> int:
    _init_tokenizer()
    text = json.dumps(messages, ensure_ascii=False)
    if _mode == "rough" or _tokenizer is None:
        return len(text) // 2
    try:
        return len(_tokenizer.encode(text))
    except Exception:
        return len(text) // 2
