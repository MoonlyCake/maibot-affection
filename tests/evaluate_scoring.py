"""本地评估仓库外的人造样例，仅输出汇总，不输出原文或样例ID。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from test_scoring_samples import summarize


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="私有 JSONL 或 JSON 样例文件")
    parser.add_argument("--require-acceptance", action="store_true", help="失败非零或中性覆盖不足80%%时返回非零")
    args = parser.parse_args()
    source = args.samples.read_text(encoding="utf-8")
    samples = (
        [json.loads(line) for line in source.splitlines() if line.strip()]
        if args.samples.suffix == ".jsonl"
        else json.loads(source)
    )
    summary = summarize(samples)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.require_acceptance and (len(samples) < 100 or not summary["acceptance_passed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
