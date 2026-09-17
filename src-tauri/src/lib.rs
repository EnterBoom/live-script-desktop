use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter};

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RunRequest {
    input_path: String,
    model_path: String,
    output_path: String,
    bucket_minutes: u32,
    threads: u32,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RunAliyunRequest {
    input_path: String,
    output_path: String,
    json_output_path: String,
    bucket_minutes: u32,
    dashscope_api_key: String,
    oss_endpoint: String,
    oss_bucket: String,
    oss_access_key_id: String,
    oss_access_key_secret: String,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct AppSettings {
    mode: Option<String>,
    model_path: Option<String>,
    bucket_minutes: Option<u32>,
    threads: Option<u32>,
    dashscope_api_key: Option<String>,
    oss_endpoint: Option<String>,
    oss_bucket: Option<String>,
    oss_access_key_id: Option<String>,
    oss_access_key_secret: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct TranscriptRow {
    time: String,
    text: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RunResponse {
    output_path: String,
    srt_path: String,
    rows: Vec<TranscriptRow>,
    log: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct HistoryEntry {
    id: String,
    created_at: String,
    mode: String,
    input_path: String,
    output_path: String,
    status: String,
    rows_count: u32,
    error_code: Option<String>,
    error_message: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ProgressEvent {
    line: String,
}

fn settings_path() -> Result<PathBuf, String> {
    Ok(app_data_dir()?.join("settings.json"))
}

fn history_path() -> Result<PathBuf, String> {
    Ok(app_data_dir()?.join("history.json"))
}

fn app_data_dir() -> Result<PathBuf, String> {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .ok_or_else(|| "无法确定用户主目录".to_string())?;
    Ok(PathBuf::from(home).join(".codex").join("live-script-desktop"))
}

fn resolve_script_path(app: &AppHandle, name: &str) -> Result<PathBuf, String> {
    use tauri::Manager;
    let mut candidates = Vec::new();
    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join("_up_").join(name));
        candidates.push(resource_dir.join(name));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            candidates.push(parent.join(name));
        }
    }
    if let Ok(current) = std::env::current_dir() {
        candidates.push(current.join(name));
        candidates.push(current.join("..").join(name));
    }
    candidates.into_iter().find(|path| path.is_file())
        .ok_or_else(|| format!("缺少脚本文件: {name}，请重新安装完整程序"))
}

fn python_command() -> Command {
    #[cfg(target_os = "windows")]
    {
        let mut command = Command::new("py");
        command.arg("-3");
        command
    }
    #[cfg(not(target_os = "windows"))]
    {
        Command::new("python3")
    }
}

#[tauri::command]
fn load_settings() -> Result<Option<AppSettings>, String> {
    let path = settings_path()?;
    if !path.exists() {
        return Ok(None);
    }
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    let settings = serde_json::from_str(&content).map_err(|error| error.to_string())?;
    Ok(Some(settings))
}

#[tauri::command]
fn save_settings(settings: AppSettings) -> Result<(), String> {
    let path = settings_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let content = serde_json::to_string_pretty(&settings).map_err(|error| error.to_string())?;
    fs::write(path, content).map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
fn clear_settings() -> Result<(), String> {
    let path = settings_path()?;
    if path.exists() {
        fs::remove_file(path).map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn load_history() -> Result<Vec<HistoryEntry>, String> {
    let path = history_path()?;
    if !path.exists() {
        return Ok(Vec::new());
    }
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    let history = serde_json::from_str(&content).map_err(|error| error.to_string())?;
    Ok(history)
}

#[tauri::command]
fn append_history(entry: HistoryEntry) -> Result<(), String> {
    let path = history_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }

    let mut history = if path.exists() {
        let content = fs::read_to_string(&path).map_err(|error| error.to_string())?;
        serde_json::from_str::<Vec<HistoryEntry>>(&content).map_err(|error| error.to_string())?
    } else {
        Vec::new()
    };

    history.insert(0, entry);
    if history.len() > 50 {
        history.truncate(50);
    }

    let content = serde_json::to_string_pretty(&history).map_err(|error| error.to_string())?;
    fs::write(path, content).map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
fn clear_history() -> Result<(), String> {
    let path = history_path()?;
    if path.exists() {
        fs::remove_file(path).map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn reveal_in_finder(path: String) -> Result<(), String> {
    let target = PathBuf::from(path);
    let directory = if target.is_dir() {
        target
    } else {
        target.parent().map(PathBuf::from).ok_or_else(|| "无效路径".to_string())?
    };

    #[cfg(target_os = "windows")]
    let mut command = Command::new("explorer");
    #[cfg(not(target_os = "windows"))]
    let mut command = Command::new("open");
    command
        .arg(directory)
        .status()
        .map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
async fn run_local_transcription(app: AppHandle, request: RunRequest) -> Result<RunResponse, String> {
    tauri::async_runtime::spawn_blocking(move || run_local_transcription_inner(app, request))
        .await
        .map_err(|error| error.to_string())?
}

fn run_local_transcription_inner(app: AppHandle, request: RunRequest) -> Result<RunResponse, String> {
    let script_path = resolve_script_path(&app, "local_live_script_tool.py")?;

    let output_path = PathBuf::from(&request.output_path);
    let mut command = python_command();
    command
        .arg(script_path)
        .arg(&request.input_path)
        .arg("--model")
        .arg(&request.model_path)
        .arg("--output")
        .arg(&request.output_path)
        .arg("--threads")
        .arg(request.threads.to_string())
        .arg("--bucket-minutes")
        .arg(request.bucket_minutes.to_string())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command.spawn().map_err(|error| error.to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法读取标准输出".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "无法读取错误输出".to_string())?;
    let log = Arc::new(Mutex::new(Vec::new()));

    let stdout_handle = spawn_reader_thread(app.clone(), stdout, log.clone());
    let stderr_handle = spawn_reader_thread(app, stderr, log.clone());
    let status = child.wait().map_err(|error| error.to_string())?;

    stdout_handle
        .join()
        .map_err(|_| "读取标准输出时发生异常".to_string())?;
    stderr_handle
        .join()
        .map_err(|_| "读取错误输出时发生异常".to_string())?;

    let log = Arc::try_unwrap(log)
        .map_err(|_| "日志状态释放失败".to_string())?
        .into_inner()
        .map_err(|_| "日志状态读取失败".to_string())?;

    if !status.success() {
        let error_lines: Vec<String> = log
            .iter()
            .filter(|line| !line.trim().is_empty())
            .cloned()
            .collect();
        return Err(error_lines.join("\n"));
    }

    let srt_path = output_path.with_extension("srt");
    let rows = if srt_path.exists() {
        parse_srt_to_rows(&srt_path, request.bucket_minutes)?
    } else {
        Vec::new()
    };

    Ok(RunResponse {
        output_path: output_path.display().to_string(),
        srt_path: srt_path.display().to_string(),
        rows,
        log,
    })
}

#[tauri::command]
async fn run_aliyun_transcription(app: AppHandle, request: RunAliyunRequest) -> Result<RunResponse, String> {
    tauri::async_runtime::spawn_blocking(move || run_aliyun_transcription_inner(app, request))
        .await
        .map_err(|error| error.to_string())?
}

fn run_aliyun_transcription_inner(app: AppHandle, request: RunAliyunRequest) -> Result<RunResponse, String> {
    let script_path = resolve_script_path(&app, "aliyun_auto_pipeline.py")?;

    let output_path = PathBuf::from(&request.output_path);
    let mut command = python_command();
    command
        .arg(script_path)
        .arg(&request.input_path)
        .arg("--dashscope-api-key")
        .arg(&request.dashscope_api_key)
        .arg("--oss-endpoint")
        .arg(&request.oss_endpoint)
        .arg("--oss-bucket")
        .arg(&request.oss_bucket)
        .arg("--oss-access-key-id")
        .arg(&request.oss_access_key_id)
        .arg("--oss-access-key-secret")
        .arg(&request.oss_access_key_secret)
        .arg("--output")
        .arg(&request.output_path)
        .arg("--json-output")
        .arg(&request.json_output_path)
        .arg("--bucket-minutes")
        .arg(request.bucket_minutes.to_string())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command.spawn().map_err(|error| error.to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法读取标准输出".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "无法读取错误输出".to_string())?;
    let log = Arc::new(Mutex::new(Vec::new()));

    let stdout_handle = spawn_reader_thread(app.clone(), stdout, log.clone());
    let stderr_handle = spawn_reader_thread(app, stderr, log.clone());
    let status = child.wait().map_err(|error| error.to_string())?;

    stdout_handle
        .join()
        .map_err(|_| "读取标准输出时发生异常".to_string())?;
    stderr_handle
        .join()
        .map_err(|_| "读取错误输出时发生异常".to_string())?;

    let log = Arc::try_unwrap(log)
        .map_err(|_| "日志状态释放失败".to_string())?
        .into_inner()
        .map_err(|_| "日志状态读取失败".to_string())?;

    if !status.success() {
        let error_lines: Vec<String> = log
            .iter()
            .filter(|line| !line.trim().is_empty())
            .cloned()
            .collect();
        return Err(error_lines.join("\n"));
    }

    let json_path = PathBuf::from(&request.json_output_path);
    let rows = if json_path.exists() {
        parse_aliyun_json_to_rows(&json_path, request.bucket_minutes)?
    } else {
        Vec::new()
    };

    Ok(RunResponse {
        output_path: output_path.display().to_string(),
        srt_path: json_path.display().to_string(),
        rows,
        log,
    })
}

fn spawn_reader_thread<R>(
    app: AppHandle,
    reader: R,
    log: Arc<Mutex<Vec<String>>>,
) -> std::thread::JoinHandle<()>
where
    R: std::io::Read + Send + 'static,
{
    std::thread::spawn(move || {
        let reader = BufReader::new(reader);
        for line in reader.lines() {
            let Ok(line) = line else {
                continue;
            };
            if let Ok(mut items) = log.lock() {
                items.push(line.clone());
            }
            let _ = app.emit(
                "transcription-log",
                ProgressEvent {
                    line,
                },
            );
        }
    })
}

fn timestamp_to_seconds(timestamp: &str) -> Result<f64, String> {
    let normalized = timestamp.replace(',', ".");
    let parts: Vec<&str> = normalized.split(':').collect();
    if parts.len() != 3 {
        return Err(format!("无效时间戳: {timestamp}"));
    }

    let hours = parts[0].parse::<u32>().map_err(|error| error.to_string())?;
    let minutes = parts[1].parse::<u32>().map_err(|error| error.to_string())?;
    let sec_parts: Vec<&str> = parts[2].split('.').collect();
    if sec_parts.len() != 2 {
        return Err(format!("无效时间戳: {timestamp}"));
    }

    let seconds = sec_parts[0]
        .parse::<u32>()
        .map_err(|error| error.to_string())?;
    let millis = sec_parts[1]
        .parse::<u32>()
        .map_err(|error| error.to_string())?;

    Ok((hours * 3600 + minutes * 60 + seconds) as f64 + millis as f64 / 1000.0)
}

fn seconds_to_label(seconds: u32) -> String {
    let hours = seconds / 3600;
    let remainder = seconds % 3600;
    let minutes = remainder / 60;
    let secs = remainder % 60;
    if hours > 0 {
        format!("{hours:02}:{minutes:02}:{secs:02}")
    } else {
        format!("{minutes:02}:{secs:02}")
    }
}

fn parse_srt_to_rows(path: &PathBuf, bucket_minutes: u32) -> Result<Vec<TranscriptRow>, String> {
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    let normalized = content.replace("\r\n", "\n");
    let mut buckets: std::collections::BTreeMap<u32, Vec<String>> = std::collections::BTreeMap::new();
    let bucket_seconds = bucket_minutes * 60;

    for block in normalized.split("\n\n") {
        let lines: Vec<&str> = block.lines().collect();
        if lines.len() < 3 {
            continue;
        }

        let timing = lines[1];
        let Some((start, _end)) = timing.split_once(" --> ") else {
            continue;
        };

        let text = lines[2..].join(" ").trim().to_string();
        if text.is_empty() {
            continue;
        }

        let bucket_index = (timestamp_to_seconds(start)? as u32) / bucket_seconds;
        buckets.entry(bucket_index).or_default().push(text);
    }

    let rows = buckets
        .into_iter()
        .map(|(bucket_index, texts)| {
            let start_seconds = bucket_index * bucket_seconds;
            let end_seconds = start_seconds + bucket_seconds;
            TranscriptRow {
                time: format!(
                    "{} - {}",
                    seconds_to_label(start_seconds),
                    seconds_to_label(end_seconds)
                ),
                text: texts.join(" "),
            }
        })
        .collect();

    Ok(rows)
}

fn parse_aliyun_json_to_rows(path: &PathBuf, bucket_minutes: u32) -> Result<Vec<TranscriptRow>, String> {
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    let payload: serde_json::Value = serde_json::from_str(&content).map_err(|error| error.to_string())?;
    let transcript = payload["transcripts"]
        .get(0)
        .ok_or_else(|| "缺少 transcripts[0]".to_string())?;
    let sentences = transcript["sentences"]
        .as_array()
        .ok_or_else(|| "缺少 sentences".to_string())?;
    let total_ms = transcript["content_duration_in_milliseconds"]
        .as_u64()
        .ok_or_else(|| "缺少 content_duration_in_milliseconds".to_string())?;

    let bucket_seconds = bucket_minutes * 60;
    let total_seconds = std::cmp::max(1, (total_ms / 1000) as u32);
    let bucket_count = std::cmp::max(1, total_seconds.div_ceil(bucket_seconds));
    let mut buckets: std::collections::BTreeMap<u32, Vec<String>> = std::collections::BTreeMap::new();

    for sentence in sentences {
        let begin_ms = sentence["begin_time"].as_u64().unwrap_or(0) as u32;
        let text = sentence["text"].as_str().unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ");
        if text.is_empty() {
            continue;
        }
        let bucket_index = std::cmp::min(begin_ms / 1000 / bucket_seconds, bucket_count - 1);
        buckets.entry(bucket_index).or_default().push(text);
    }

    let mut rows = Vec::new();
    for bucket_index in 0..bucket_count {
        let text = buckets.get(&bucket_index).map(|items| items.join(" ")).unwrap_or_default();
        if text.is_empty() {
            continue;
        }
        let start_seconds = bucket_index * bucket_seconds;
        let end_seconds = std::cmp::min((bucket_index + 1) * bucket_seconds, total_seconds);
        rows.push(TranscriptRow {
            time: format!("{} - {}", seconds_to_label(start_seconds), seconds_to_label(end_seconds)),
            text,
        });
    }
    Ok(rows)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            run_local_transcription,
            run_aliyun_transcription,
            load_settings,
            save_settings,
            clear_settings,
            load_history,
            append_history,
            clear_history,
            reveal_in_finder
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
