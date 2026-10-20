"""Pinned offline DeepSeek V4 tokenizer; conservative text-only wire bound."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from benchmark.t237.budget import BudgetError

_ASSETS = {
    "tokenizer.json": "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614",
    "tokenizer_config.json": "841f8cf146e3f0ad1082594a31f68ecf7608c20467ef355081333d85bbaeb1cb",
}


class OfflineInputBound:
    def __init__(self, root: Path):
        for name, digest in _ASSETS.items():
            try:
                matches = hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            except OSError:
                matches = False
            if not matches:
                raise BudgetError("TOKENIZER_ASSET_UNVERIFIED")
        from transformers import AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(
            str(root), local_files_only=True, trust_remote_code=False,
        )

    def __call__(self, body: dict[str, Any]) -> int:
        rendered = self._tokenizer.apply_chat_template(
            body["messages"], tokenize=False, add_generation_prompt=True,
        )
        if not isinstance(rendered, str):
            raise BudgetError("TOKENIZER_TEMPLATE_INVALID")
        tokens = len(self._tokenizer.encode(rendered, add_special_tokens=False))
        # Pinned tokenizer is byte-level BPE without expanding normalization.
        # Bound every rendered UTF-8 byte plus 256 tokens of framing margin.
        # This deliberately overestimates, never truncates the business prompt.
        return max(tokens, len(rendered.encode("utf-8"))) + 256
