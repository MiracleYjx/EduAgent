"""初始化可重复评测的隔离语料；或显式清理本工具生成的 schema。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.benchmark_corpus import CORPUS_DIR, cleanup_manifest, setup_corpus
from scripts.benchmark_runtime import safe_error_detail
from scripts.run_retrieval_benchmark import _make_embedding_provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output-dir", type=Path, help="必须是新目录；保留 manifest 与隔离语料。")
    action.add_argument("--cleanup", type=Path, help="manifest 路径；删除对应隔离 schema，保留报告。")
    parser.add_argument("--corpus-dir", type=Path, default=CORPUS_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--self-test", action="store_true", default=True, help="默认使用 stub Embedding，仅验证管道。")
    mode.add_argument("--real", dest="self_test", action="store_false", help="显式启用已授权真实 Embedding。")
    args = parser.parse_args(argv)
    try:
        if args.cleanup:
            cleanup_manifest(args.cleanup)
            print("已删除该 manifest 对应的 Benchmark schema；报告保留，语料需重新摄取。")
        else:
            path = setup_corpus(
                args.output_dir, corpus_dir=args.corpus_dir,
                provider=_make_embedding_provider(args.self_test), close_provider=True,
            )
            print(f"manifest: {path}")
    except Exception as exc:  # noqa: BLE001 - CLI 明确失败，不回退到 public/替身
        detail = safe_error_detail(exc)
        print(f"Benchmark 初始化/清理失败：{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
