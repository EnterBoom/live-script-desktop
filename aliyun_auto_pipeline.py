#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import shutil
from datetime import datetime
from pathlib import Path

from live_script_tool import write_xlsx

try:
    import oss2
except ImportError:  # pragma: no cover
    oss2 = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="自动上传 OSS，调用阿里云百炼转写，并导出分钟级 Excel。"
    )
    parser.add_argument("input", help="输入视频或音频文件路径")
    parser.add_argument("--dashscope-api-key", required=True, help="阿里云百炼 API Key")
    parser.add_argument("--oss-endpoint", required=True, help="OSS endpoint，例如 oss-cn-beijing.aliyuncs.com")
    parser.add_argument("--oss-bucket", required=True, help="OSS bucket 名")
    parser.add_argument("--oss-access-key-id", required=True, help="OSS AccessKey ID")
    parser.add_argument("--oss-access-key-secret", required=True, help="OSS AccessKey Secret")
    parser.add_argument("--output", required=True, help="输出 Excel 路径")
    parser.add_argument("--json-output", required=True, help="输出阿里云原始 JSON 路径")
    parser.add_argument("--bucket-minutes", type=int, default=1, help="按多少分钟聚合成一行")
    parser.add_argument("--language", default="zh", help="语言提示，默认 zh")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg 可执行文件或路径")
    parser.add_argument("--keep-audio", action="store_true", help="保留中间音频文件")
    return parser.parse_args()


def run_command(command: list[str], label: str) -> None:
    import subprocess

    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"{label}失败：{completed.stderr.strip() or completed.stdout.strip()}")


def ensure_parent_writable(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    probe_path = path.parent / f".{path.stem}.write-test"
    try:
        probe_path.write_text("ok", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"{label}不可写：{path.parent}，{exc}") from exc
    finally:
        if probe_path.exists():
            probe_path.unlink()


def validate_runtime(args: argparse.Namespace, input_path: Path, output_path: Path, json_output_path: Path) -> None:
    if shutil.which(args.ffmpeg_bin) is None and not Path(args.ffmpeg_bin).exists():
        raise SystemExit(f"找不到 ffmpeg：{args.ffmpeg_bin}")
    if oss2 is None:
        raise SystemExit("缺少 oss2，请先执行 `python3 -m pip install oss2`。")
    ensure_parent_writable(output_path, "Excel 输出目录")
    ensure_parent_writable(json_output_path, "JSON 输出目录")
    if output_path.exists() and not os.access(output_path, os.W_OK):
        raise SystemExit(f"Excel 输出文件不可写：{output_path}")
    if json_output_path.exists() and not os.access(json_output_path, os.W_OK):
        raise SystemExit(f"JSON 输出文件不可写：{json_output_path}")
    if not input_path.exists():
        raise SystemExit(f"输入文件不存在：{input_path}")


def extract_audio(input_path: Path, audio_path: Path, ffmpeg_bin: str) -> None:
    print("[1/5] 提取音频...", file=sys.stderr, flush=True)
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
            "libopus",
            str(audio_path),
        ],
        "ffmpeg 提取音频",
    )


def upload_to_oss(
    audio_path: Path,
    endpoint: str,
    bucket_name: str,
    access_key_id: str,
    access_key_secret: str,
) -> str:
    print("[2/5] 上传到 OSS...", file=sys.stderr, flush=True)
    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, f"https://{endpoint}", bucket_name)
    timestamp = datetime.now().strftime("%Y%m%d/%H%M%S")
    object_key = f"live-script/{timestamp}-{audio_path.name}"
    bucket.put_object_from_file(object_key, str(audio_path))
    signed_url = bucket.sign_url("GET", object_key, 24 * 3600)
    return signed_url


def submit_transcription_task(api_key: str, file_url: str, language: str) -> str:
    print("[3/5] 提交百炼转写任务...", file=sys.stderr, flush=True)
    payload = {
        "model": "paraformer-v2",
        "input": {"file_urls": [file_url]},
        "parameters": {
            "language_hints": [language],
            "timestamp_alignment_enabled": True,
            "disfluency_removal_enabled": False,
        },
    }
    request = urllib.request.Request(
        "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data["output"]["task_id"]
    except urllib.error.HTTPError as exc:
        raise SystemExit(exc.read().decode("utf-8", errors="replace")) from exc


def poll_task(api_key: str, task_id: str) -> dict:
    print("[4/5] 等待百炼完成...", file=sys.stderr, flush=True)
    url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
    while True:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SystemExit(exc.read().decode("utf-8", errors="replace")) from exc

        output = payload.get("output", {})
        status = output.get("task_status")
        print(f"任务状态：{status}", file=sys.stderr, flush=True)
        if status == "SUCCEEDED":
            return payload
        if status == "FAILED":
            raise SystemExit(json.dumps(payload, ensure_ascii=False))
        time.sleep(8)


def download_result(task_payload: dict) -> dict:
    result_url = task_payload["output"]["results"][0]["transcription_url"]
    with urllib.request.urlopen(result_url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def seconds_to_label(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def aggregate_sentences(sentences: list[dict], total_ms: int, bucket_minutes: int) -> list[list[str]]:
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
        buckets[bucket_index].append(text)

    rows = [["时间节点", "对应话术"]]
    for bucket_index in range(bucket_count):
        merged = " ".join(buckets.get(bucket_index, [])).strip()
        if not merged:
            continue
        start_seconds = bucket_index * bucket_seconds
        end_seconds = min((bucket_index + 1) * bucket_seconds, total_seconds)
        rows.append(
            [
                f"{seconds_to_label(start_seconds)} - {seconds_to_label(end_seconds)}",
                merged,
            ]
        )
    return rows


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    json_output_path = Path(args.json_output).expanduser().resolve()
    validate_runtime(args, input_path, output_path, json_output_path)

    with tempfile.TemporaryDirectory(prefix="aliyun-live-script-") as temp_dir:
        audio_path = Path(temp_dir) / f"{input_path.stem}.opus"
        extract_audio(input_path, audio_path, args.ffmpeg_bin)
        signed_url = upload_to_oss(
            audio_path=audio_path,
            endpoint=args.oss_endpoint,
            bucket_name=args.oss_bucket,
            access_key_id=args.oss_access_key_id,
            access_key_secret=args.oss_access_key_secret,
        )
        task_id = submit_transcription_task(
            api_key=args.dashscope_api_key,
            file_url=signed_url,
            language=args.language,
        )
        task_payload = poll_task(args.dashscope_api_key, task_id)
        result_payload = download_result(task_payload)

        if args.keep_audio:
            keep_path = output_path.with_suffix(".opus")
            keep_path.write_bytes(audio_path.read_bytes())

    json_output_path.parent.mkdir(parents=True, exist_ok=True)
    json_output_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    transcript = result_payload["transcripts"][0]
    rows = aggregate_sentences(
        transcript.get("sentences", []),
        int(transcript.get("content_duration_in_milliseconds", 0)),
        args.bucket_minutes,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(output_path, rows)

    print(f"Excel 已生成：{output_path}")
    print(f"JSON 已生成：{json_output_path}")


if __name__ == "__main__":
    main()
