#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from live_script_tool import write_xlsx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把阿里云百炼转写 JSON 转成按分钟聚合的 Excel。"
    )
    parser.add_argument("input", help="阿里云转写 JSON 文件路径")
    parser.add_argument(
        "--output",
        required=True,
        help="输出 Excel 路径",
    )
    parser.add_argument(
        "--bucket-minutes",
        type=int,
        default=1,
        help="按多少分钟聚合成一行，默认 1",
    )
    return parser.parse_args()


def seconds_to_label(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def chunk_text_by_bucket(
    sentences: list[dict], total_ms: int, bucket_minutes: int
) -> list[tuple[str, str]]:
    bucket_seconds = bucket_minutes * 60
    total_seconds = max(1, total_ms // 1000)
    bucket_count = max(1, (total_seconds + bucket_seconds - 1) // bucket_seconds)
    buckets: dict[int, list[str]] = {index: [] for index in range(bucket_count)}

    for sentence in sentences:
        begin_ms = int(sentence.get("begin_time", 0))
        text = " ".join(str(sentence.get("text", "")).split()).strip()
        if not text:
            continue
        bucket_index = min(begin_ms // 1000 // bucket_seconds, bucket_count - 1)
        buckets.setdefault(bucket_index, []).append(text)

    rows = []
    for bucket_index in range(bucket_count):
        start_seconds = bucket_index * bucket_seconds
        end_seconds = min((bucket_index + 1) * bucket_seconds, total_seconds)
        segment = " ".join(buckets.get(bucket_index, [])).strip()
        if segment:
            rows.append(
                (
                    f"{seconds_to_label(start_seconds)} - {seconds_to_label(end_seconds)}",
                    segment,
                )
            )
    return rows


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    transcripts = payload.get("transcripts", [])
    if not transcripts:
        raise SystemExit("JSON 中没有 transcripts 字段。")

    transcript = transcripts[0]
    duration_ms = int(transcript.get("content_duration_in_milliseconds", 0))
    sentences = transcript.get("sentences", [])
    rows = chunk_text_by_bucket(sentences, duration_ms, args.bucket_minutes)
    if not rows:
        raise SystemExit("无法从阿里云结果中生成分钟稿。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(output_path, [["时间节点", "对应话术"], *rows])
    print(f"Excel 已生成：{output_path}")
    print(f"共导出 {len(rows)} 条分段。")


if __name__ == "__main__":
    main()
