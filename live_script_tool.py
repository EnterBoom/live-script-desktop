#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.sax.saxutils
import zipfile
from pathlib import Path


API_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
SUPPORTED_UPLOAD_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把视频/音频转成带时间节点的话术 Excel。"
    )
    parser.add_argument("input", help="输入视频或音频文件路径")
    parser.add_argument(
        "-o",
        "--output",
        help="输出 Excel 路径，默认与输入文件同名 .xlsx",
    )
    parser.add_argument(
        "--json-output",
        help="额外保存原始 verbose_json 转写结果",
    )
    parser.add_argument(
        "--prompt",
        help="给转写模型的提示词，例如“保留口语停顿词，适合直播稿整理”",
    )
    parser.add_argument(
        "--language",
        help="可选语言代码，例如 zh。留空则自动识别。",
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default="ffmpeg",
        help="ffmpeg 可执行文件路径，默认使用环境变量中的 ffmpeg",
    )
    return parser.parse_args()


def require_api_key() -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("缺少 OPENAI_API_KEY 环境变量。")
    return api_key


def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_time_label(start: float, end: float) -> str:
    return f"{format_timestamp(start)} - {format_timestamp(end)}"


def detect_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    return mime_type or "application/octet-stream"


def file_needs_conversion(path: Path) -> bool:
    return path.suffix.lower() not in SUPPORTED_UPLOAD_EXTENSIONS


def ffmpeg_available(ffmpeg_bin: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def split_with_ffmpeg(input_path: Path, ffmpeg_bin: str, workdir: Path) -> list[Path]:
    if not ffmpeg_available(ffmpeg_bin):
        raise SystemExit(
            "当前文件需要 ffmpeg 做音频提取/分段，但系统里找不到 ffmpeg。"
        )

    pattern = workdir / "chunk_%03d.m4a"
    command = [
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
        "aac",
        "-b:a",
        "64k",
        "-f",
        "segment",
        "-segment_time",
        "1200",
        str(pattern),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"ffmpeg 处理失败：{completed.stderr.strip()}")

    chunks = sorted(workdir.glob("chunk_*.m4a"))
    if not chunks:
        raise SystemExit("ffmpeg 未生成可转写的音频分段。")
    return chunks


def prepare_inputs(input_path: Path, ffmpeg_bin: str) -> list[tuple[Path, float]]:
    input_size = input_path.stat().st_size
    if not file_needs_conversion(input_path) and input_size <= MAX_UPLOAD_BYTES:
        return [(input_path, 0.0)]

    with tempfile.TemporaryDirectory(prefix="live-script-") as temp_dir:
        temp_path = Path(temp_dir)
        chunks = split_with_ffmpeg(input_path, ffmpeg_bin, temp_path)
        prepared = []
        elapsed = 0.0
        for chunk in chunks:
            prepared.append((chunk, elapsed))
            elapsed += probe_duration(chunk, ffmpeg_bin)
        copied_dir = Path(tempfile.mkdtemp(prefix="live-script-ready-"))
        result = []
        for chunk, offset in prepared:
            destination = copied_dir / chunk.name
            destination.write_bytes(chunk.read_bytes())
            result.append((destination, offset))
        return result


def probe_duration(path: Path, ffmpeg_bin: str) -> float:
    ffprobe_bin = str(Path(ffmpeg_bin).with_name("ffprobe"))
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"ffprobe 读取时长失败：{completed.stderr.strip()}")
    try:
        return float(completed.stdout.strip())
    except ValueError as exc:
        raise SystemExit("ffprobe 返回了无效的时长信息。") from exc


def encode_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    line_break = b"\r\n"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}".encode())
        body.extend(line_break)
        body.extend(
            f'Content-Disposition: form-data; name="{name}"'.encode()
        )
        body.extend(line_break)
        body.extend(line_break)
        body.extend(value.encode("utf-8"))
        body.extend(line_break)

    body.extend(f"--{boundary}".encode())
    body.extend(line_break)
    body.extend(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode()
    )
    body.extend(line_break)
    body.extend(f"Content-Type: {detect_mime_type(file_path)}".encode())
    body.extend(line_break)
    body.extend(line_break)
    body.extend(file_path.read_bytes())
    body.extend(line_break)
    body.extend(f"--{boundary}--".encode())
    body.extend(line_break)

    return bytes(body), boundary


def transcribe_file(
    file_path: Path,
    api_key: str,
    prompt: str | None,
    language: str | None,
    attempt_count: int = 5,
) -> dict:
    last_error = None
    for attempt in range(1, attempt_count + 1):
        fields = {
            "model": "whisper-1",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if prompt:
            fields["prompt"] = prompt
        if language:
            fields["language"] = language

        payload, boundary = encode_multipart(fields, file_path)
        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            if exc.code in {429, 500, 502, 503, 504} and attempt < attempt_count:
                wait_seconds = min(2 ** attempt, 30)
                print(
                    f"[重试] 分段 {file_path.name} 请求失败 HTTP {exc.code}，"
                    f"{wait_seconds} 秒后重试（第 {attempt}/{attempt_count} 次）。",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_seconds)
                last_error = f"HTTP {exc.code} {details}"
                continue
            raise SystemExit(f"转写请求失败：HTTP {exc.code} {details}") from exc
        except urllib.error.URLError as exc:
            if attempt < attempt_count:
                wait_seconds = min(2 ** attempt, 30)
                print(
                    f"[重试] 分段 {file_path.name} 网络异常：{exc.reason}，"
                    f"{wait_seconds} 秒后重试（第 {attempt}/{attempt_count} 次）。",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_seconds)
                last_error = str(exc.reason)
                continue
            raise SystemExit(f"无法连接转写接口：{exc.reason}") from exc

    raise SystemExit(f"转写请求失败：{last_error or '未知错误'}")


def merge_segments(transcripts: list[tuple[dict, float]]) -> list[dict]:
    merged = []
    for payload, offset in transcripts:
        for segment in payload.get("segments", []):
            merged.append(
                {
                    "start": float(segment["start"]) + offset,
                    "end": float(segment["end"]) + offset,
                    "text": segment.get("text", "").strip(),
                }
            )
    return merged


def column_name(index: int) -> str:
    result = []
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result.append(chr(65 + remainder))
    return "".join(reversed(result))


def xml_escape(value: str) -> str:
    return xml.sax.saxutils.escape(value)


def make_cell(ref: str, value: str, style: int | None = None) -> str:
    style_attr = f' s="{style}"' if style is not None else ""
    return (
        f'<c r="{ref}" t="inlineStr"{style_attr}>'
        f"<is><t>{xml_escape(value)}</t></is>"
        f"</c>"
    )


def make_row(row_index: int, values: list[str], style: int | None = None) -> str:
    cells = []
    for idx, value in enumerate(values, start=1):
        cells.append(make_cell(f"{column_name(idx)}{row_index}", value, style))
    return f'<row r="{row_index}">{"".join(cells)}</row>'


def build_sheet_xml(rows: list[list[str]]) -> str:
    xml_rows = [make_row(1, rows[0], style=1)]
    for row_index, values in enumerate(rows[1:], start=2):
        xml_rows.append(make_row(row_index, values, style=2))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="18"/>'
        '<cols>'
        '<col min="1" max="1" width="18" customWidth="1"/>'
        '<col min="2" max="2" width="90" customWidth="1"/>'
        "</cols>"
        f'<sheetData>{"".join(xml_rows)}</sheetData>'
        "</worksheet>"
    )


def build_workbook_files(rows: list[list[str]]) -> dict[str, str]:
    return {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" '
            'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
            'Target="docProps/app.xml"/>'
            "</Relationships>"
        ),
        "docProps/app.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>Codex</Application>"
            "</Properties>"
        ),
        "docProps/core.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:title>直播稿转写</dc:title>"
            "<dc:creator>Codex</dc:creator>"
            "</cp:coreProperties>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="直播稿" sheetId="1" r:id="rId1"/>'
            "</sheets>"
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2">'
            '<font><sz val="11"/><name val="Arial"/></font>'
            '<font><b/><sz val="11"/><name val="Arial"/></font>'
            "</fonts>"
            '<fills count="2">'
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            "</fills>"
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="3">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0">'
            '<alignment horizontal="center" vertical="center" wrapText="1"/>'
            "</xf>"
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0">'
            '<alignment vertical="top" wrapText="1"/>'
            "</xf>"
            "</cellXfs>"
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        ),
        "xl/worksheets/sheet1.xml": build_sheet_xml(rows),
    }


def write_xlsx(output_path: Path, rows: list[list[str]]) -> None:
    workbook_files = build_workbook_files(rows)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for relative_path, content in workbook_files.items():
            zf.writestr(relative_path, content)


def default_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".xlsx")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"输入文件不存在：{input_path}")

    api_key = require_api_key()
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path(input_path)
    )

    prepared_inputs = prepare_inputs(input_path, args.ffmpeg_bin)
    transcripts = []
    raw_results = []
    total_chunks = len(prepared_inputs)
    for index, (chunk_path, offset) in enumerate(prepared_inputs, start=1):
        print(
            f"[处理中] 第 {index}/{total_chunks} 段：{chunk_path.name}，起始偏移 {format_timestamp(offset)}",
            file=sys.stderr,
            flush=True,
        )
        payload = transcribe_file(
            file_path=chunk_path,
            api_key=api_key,
            prompt=args.prompt,
            language=args.language,
        )
        transcripts.append((payload, offset))
        raw_results.append({"offset": offset, "result": payload})

    segments = merge_segments(transcripts)
    if not segments:
        raise SystemExit("转写结果为空，没有可导出的分段。")

    rows = [["时间节点", "对应话术"]]
    for segment in segments:
        rows.append([build_time_label(segment["start"], segment["end"]), segment["text"]])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(output_path, rows)

    if args.json_output:
        json_path = Path(args.json_output).expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(raw_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"Excel 已生成：{output_path}")
    print(f"共导出 {len(segments)} 条分段。")


if __name__ == "__main__":
    main()
