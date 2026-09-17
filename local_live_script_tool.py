#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from live_script_tool import write_xlsx


SRT_BLOCK_RE = re.compile(
    r"^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL | re.MULTILINE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地提取音频并用 whisper.cpp 转成带时间节点的话术 Excel。"
    )
    parser.add_argument("input", help="输入视频或音频文件路径")
    parser.add_argument(
        "--model",
        default="ggml-small.bin",
        help="whisper.cpp 模型文件路径",
    )
    parser.add_argument(
        "--output",
        help="输出 Excel 路径，默认与输入文件同名 .xlsx",
    )
    parser.add_argument(
        "--language",
        default="zh",
        help="whisper 语言，默认 zh",
    )
    parser.add_argument(
        "--whisper-bin",
        default="whisper-cli",
        help="whisper-cli 路径",
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default="ffmpeg",
        help="ffmpeg 路径",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="whisper 线程数，默认 4",
    )
    parser.add_argument(
        "--bucket-minutes",
        type=int,
        default=1,
        help="按多少分钟聚合成一行，默认 1",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="启用 GPU。默认关闭，优先保证兼容性。",
    )
    return parser.parse_args()


def run_command(command: list[str], label: str) -> None:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"{label}失败：{completed.stderr.strip() or completed.stdout.strip()}")


def extract_audio(input_path: Path, wav_path: Path, ffmpeg_bin: str) -> None:
    print("[1/3] 提取音频...", file=sys.stderr, flush=True)
    run_command(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        "ffmpeg 音频提取",
    )


def transcribe_audio(
    wav_path: Path,
    output_prefix: Path,
    whisper_bin: str,
    model_path: Path,
    language: str,
    threads: int,
    use_gpu: bool,
) -> Path:
    print("[2/3] 本地转写并生成时间戳...", file=sys.stderr, flush=True)
    command = [
        whisper_bin,
        "-m",
        str(model_path),
        "-f",
        str(wav_path),
        "-l",
        language,
        "-t",
        str(threads),
        "-osrt",
        "-of",
        str(output_prefix),
    ]
    if not use_gpu:
        command.append("-ng")
    run_command(command, "whisper 本地转写")
    srt_path = output_prefix.with_suffix(".srt")
    if not srt_path.exists():
        raise SystemExit(f"转写完成但未找到 SRT 输出：{srt_path}")
    return srt_path


def run_transcription_with_fallback(
    wav_path: Path,
    output_prefix: Path,
    whisper_bin: str,
    model_path: Path,
    language: str,
    threads: int,
    prefer_gpu: bool,
) -> Path:
    if prefer_gpu:
        try:
            print("[2/3] 先尝试 GPU 转写...", file=sys.stderr, flush=True)
            return transcribe_audio(
                wav_path=wav_path,
                output_prefix=output_prefix,
                whisper_bin=whisper_bin,
                model_path=model_path,
                language=language,
                threads=threads,
                use_gpu=True,
            )
        except SystemExit as exc:
            print(
                f"[回退] GPU 转写失败，改用 CPU：{exc}",
                file=sys.stderr,
                flush=True,
            )

    print("[2/3] 使用 CPU 转写并生成时间戳...", file=sys.stderr, flush=True)
    return transcribe_audio(
        wav_path=wav_path,
        output_prefix=output_prefix,
        whisper_bin=whisper_bin,
        model_path=model_path,
        language=language,
        threads=threads,
        use_gpu=False,
    )


def parse_srt_blocks(srt_text: str) -> list[tuple[str, str]]:
    rows = []
    normalized = srt_text.replace("\r\n", "\n")
    for match in SRT_BLOCK_RE.finditer(normalized):
        start = match.group(2).replace(",", ".")
        end = match.group(3).replace(",", ".")
        text = re.sub(r"\s+", " ", match.group(4)).strip()
        if text:
            rows.append((f"{start} - {end}", text))
    return rows


def timestamp_to_seconds(timestamp: str) -> float:
    hours, minutes, seconds_millis = timestamp.split(":")
    seconds, millis = seconds_millis.split(".")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000
    )


def seconds_to_label(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def aggregate_rows_by_minute(
    rows: list[tuple[str, str]], bucket_minutes: int
) -> list[tuple[str, str]]:
    bucket_seconds = bucket_minutes * 60
    buckets: dict[int, list[str]] = {}
    for time_label, text in rows:
        start_text = time_label.split(" - ", 1)[0]
        bucket_index = int(timestamp_to_seconds(start_text) // bucket_seconds)
        buckets.setdefault(bucket_index, []).append(text)

    aggregated = []
    for bucket_index in sorted(buckets):
        start_seconds = bucket_index * bucket_seconds
        end_seconds = start_seconds + bucket_seconds
        merged_text = " ".join(buckets[bucket_index]).strip()
        if merged_text:
            aggregated.append(
                (
                    f"{seconds_to_label(start_seconds)} - {seconds_to_label(end_seconds)}",
                    merged_text,
                )
            )
    return aggregated


def default_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".xlsx")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    model_path = Path(args.model).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"输入文件不存在：{input_path}")
    if not model_path.exists():
        raise SystemExit(f"模型文件不存在：{model_path}")

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path(input_path)
    )
    output_prefix = output_path.with_suffix("")

    with tempfile.TemporaryDirectory(prefix="local-live-script-") as temp_dir:
        wav_path = Path(temp_dir) / f"{input_path.stem}.wav"
        extract_audio(input_path, wav_path, args.ffmpeg_bin)
        srt_path = run_transcription_with_fallback(
            wav_path=wav_path,
            output_prefix=output_prefix,
            whisper_bin=args.whisper_bin,
            model_path=model_path,
            language=args.language,
            threads=args.threads,
            prefer_gpu=args.gpu,
        )
        rows = parse_srt_blocks(srt_path.read_text(encoding="utf-8"))

    if not rows:
        raise SystemExit("没有从 SRT 中解析出任何转写结果。")
    rows = aggregate_rows_by_minute(rows, args.bucket_minutes)

    print("[3/3] 导出 Excel...", file=sys.stderr, flush=True)
    write_xlsx(output_path, [["时间节点", "对应话术"], *rows])
    print(f"Excel 已生成：{output_path}")
    print(f"共导出 {len(rows)} 条分段。")


if __name__ == "__main__":
    main()
