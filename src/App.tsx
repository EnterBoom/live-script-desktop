import { useEffect, useMemo, useState } from "react";
import { open, save } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";

type TranscriptRow = {
  time: string;
  text: string;
};

type RunResponse = {
  outputPath: string;
  srtPath: string;
  rows: TranscriptRow[];
  log: string[];
};

type Mode = "aliyun" | "local";

type AppState = {
  mode: Mode;
  inputPath: string;
  modelPath: string;
  outputPath: string;
  jsonOutputPath: string;
  bucketMinutes: number;
  threads: number;
  dashscopeApiKey: string;
  ossEndpoint: string;
  ossBucket: string;
  ossAccessKeyId: string;
  ossAccessKeySecret: string;
};

type ErrorGuide = {
  code: string;
  title: string;
  reason: string;
  fix: string;
};

type ProgressPayload = {
  line: string;
};

type HistoryEntry = {
  id: string;
  createdAt: string;
  mode: Mode;
  inputPath: string;
  outputPath: string;
  status: string;
  rowsCount: number;
  errorCode?: string | null;
  errorMessage?: string | null;
};

type QueueTask = {
  id: string;
  mode: Mode;
  inputPath: string;
  outputPath: string;
  jsonOutputPath: string;
  status: "待开始" | "处理中" | "已完成" | "失败";
  progress: number;
  stepLabel: string;
  previewRows: TranscriptRow[];
  log: string[];
};

type PreviewPayload = {
  title: string;
  rows: TranscriptRow[];
  log: string[];
};

const defaultState: AppState = {
  mode: "aliyun",
  inputPath: "",
  modelPath: "",
  outputPath: "",
  jsonOutputPath: "",
  bucketMinutes: 1,
  threads: 4,
  dashscopeApiKey: "",
  ossEndpoint: "oss-cn-beijing.aliyuncs.com",
  ossBucket: "",
  ossAccessKeyId: "",
  ossAccessKeySecret: "",
};

const previewStoragePrefix = "live-script-preview:";

function getErrorGuide(message: string): ErrorGuide {
  const normalized = message.toLowerCase();

  if (normalized.includes("打开文件选择器失败") || normalized.includes("dialog")) {
    return {
      code: "E001",
      title: "文件选择窗口没有正常打开",
      reason: "通常是窗口权限、弹窗能力或当前窗口状态异常。",
      fix: "先关闭软件重新打开，再点一次“选择”；如果还是不行，重启电脑后再试。",
    };
  }

  if (normalized.includes("ffmpeg")) {
    return {
      code: "E002",
      title: "视频转音频失败",
      reason: "软件在提取音频时失败了，常见原因是视频文件格式异常或文件损坏。",
      fix: "先确认视频本身可以正常播放；如果能播放，换一个视频文件再试一次。",
    };
  }

  if (normalized.includes("oss")) {
    if (normalized.includes("ssl") || normalized.includes("unexpected_eof_while_reading")) {
      return {
        code: "E003",
        title: "上传阿里云存储失败",
        reason: "你填写的 OSS 地域地址很可能不完整，导致 SSL 连接失败。",
        fix: "把 OSS 地域地址改成完整域名，例如 `oss-cn-hangzhou.aliyuncs.com`，不要只填 `oss-cn-hangzhou`。",
      };
    }
    return {
      code: "E003",
      title: "上传阿里云存储失败",
      reason: "通常是 OSS 地域、存储桶名称、存储账号 ID 或存储账号密钥填错。",
      fix: "重点检查 OSS 地域地址、存储桶名称、阿里云存储账号 ID、阿里云存储账号密钥。",
    };
  }

  if (normalized.includes("dashscope") || normalized.includes("task_status") || normalized.includes("failed")) {
    return {
      code: "E004",
      title: "阿里云百炼转写失败",
      reason: "通常是百炼 API 密钥错误、OSS 文件无法读取，或者百炼服务端任务失败。",
      fix: "先检查百炼 API 密钥是否正确，再检查 OSS 上传是否成功。",
    };
  }

  if (normalized.includes("model")) {
    return {
      code: "E005",
      title: "本地模型文件有问题",
      reason: "通常是模型文件路径不对，或者模型文件没有下载完整。",
      fix: "重新选择 `.bin` 模型文件，确认文件存在并且大小正常。",
    };
  }

  if (normalized.includes("xlsx") || normalized.includes("excel") || normalized.includes("permissionerror")) {
    return {
      code: "E006",
      title: "导出 Excel 失败",
      reason: "通常是导出路径没有权限，或者目标 Excel 文件正在被占用。",
      fix: "换一个导出路径，或者先关闭已经打开的同名 Excel 文件后再试。",
    };
  }

  return {
    code: "E999",
    title: "未知错误",
    reason: "软件遇到了一个未分类的问题。",
    fix: "把原始报错内容发给我，我可以继续帮你定位。",
  };
}

function getStatusFromLine(line: string, mode: Mode): string | null {
  if (line.includes("[1/5]") || line.includes("[1/3]")) return "正在提取音频...";
  if (line.includes("[2/5]")) return "正在上传到 OSS...";
  if (line.includes("[3/5]")) return "正在提交百炼任务...";
  if (line.includes("[4/5]")) return "正在等待百炼识别完成...";
  if (line.includes("[5/5]") || line.includes("[3/3]")) return "正在导出 Excel...";
  if (line.includes("任务状态：")) return line;
  if (mode === "local" && line.includes("GPU 转写失败")) return "GPU 不稳定，已自动回退到 CPU...";
  if (mode === "local" && line.includes("使用 CPU 转写")) return "正在使用 CPU 本地转写...";
  return null;
}

function getStepFromLine(line: string, mode: Mode): number | null {
  if (line.includes("[1/5]") || line.includes("[1/3]")) return 1;
  if (line.includes("[2/5]")) return 2;
  if (line.includes("[3/5]")) return 3;
  if (line.includes("[4/5]") || line.includes("任务状态：")) return 4;
  if (line.includes("[5/5]") || line.includes("[3/3]")) return 5;
  if (mode === "local" && line.includes("[2/3]")) return 2;
  return null;
}

function getStepLabel(mode: Mode, activeStep: number): string {
  if (activeStep <= 0) return "待开始";
  if (mode === "aliyun") {
    return ["提取音频", "上传 OSS", "提交百炼", "等待识别", "导出 Excel"][activeStep - 1] ?? "处理中";
  }
  return ["提取音频", "本地识别", "整理时间", "写入结果", "导出 Excel"][activeStep - 1] ?? "处理中";
}

function buildTaskPaths(inputPath: string, mode: Mode) {
  return {
    outputPath: inputPath.replace(/\.[^.]+$/, mode === "aliyun" ? "-阿里云分钟稿.xlsx" : ".xlsx"),
    jsonOutputPath: inputPath.replace(/\.[^.]+$/, ".aliyun.json"),
  };
}

function formatHistoryTime(value: string): string {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function getPreviewTaskIdFromUrl() {
  return new URLSearchParams(window.location.search).get("previewTaskId");
}

function PreviewWindow() {
  const taskId = getPreviewTaskIdFromUrl();
  const [payload, setPayload] = useState<PreviewPayload | null>(null);

  useEffect(() => {
    if (!taskId) return;
    const raw = localStorage.getItem(`${previewStoragePrefix}${taskId}`);
    if (!raw) return;
    setPayload(JSON.parse(raw) as PreviewPayload);
  }, [taskId]);

  return (
    <div className="preview-shell">
      <section className="preview-hero">
        <p className="eyebrow">Preview Window</p>
        <h1>分钟稿预览</h1>
        <p className="subcopy">{payload?.title ?? "没有可显示的任务内容"}</p>
      </section>
      <section className="panel-grid preview-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>分钟稿</h2>
            <span>{payload?.rows.length ?? 0} 条</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>时间节点</th>
                  <th>对应话术</th>
                </tr>
              </thead>
              <tbody>
                {!payload?.rows.length ? (
                  <tr>
                    <td colSpan={2} className="empty-cell">
                      当前任务还没有可预览的分钟稿。
                    </td>
                  </tr>
                ) : (
                  payload.rows.map((row) => (
                    <tr key={`${row.time}-${row.text.slice(0, 12)}`}>
                      <td>{row.time}</td>
                      <td>{row.text}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
        <div className="panel">
          <div className="panel-header">
            <h2>执行日志</h2>
            <span>{payload?.log.length ?? 0} 条</span>
          </div>
          <div className="log-list preview-log">
            {!payload?.log.length ? (
              <p className="empty-cell">当前任务还没有日志。</p>
            ) : (
              payload.log.map((entry, index) => <code key={`${entry}-${index}`}>{entry}</code>)
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const previewTaskId = getPreviewTaskIdFromUrl();
  if (previewTaskId) {
    return <PreviewWindow />;
  }

  const [form, setForm] = useState<AppState>(defaultState);
  const [status, setStatus] = useState("等待开始");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [copiedError, setCopiedError] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [taskQueue, setTaskQueue] = useState<QueueTask[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [activeTaskId, setActiveTaskId] = useState("");
  const [liveLog, setLiveLog] = useState<string[]>([]);
  const errorGuide = error ? getErrorGuide(error) : null;

  const currentTask = taskQueue.find((item) => item.id === selectedTaskId) ?? null;
  const successCount = history.filter((item) => item.status === "已完成").length;
  const failedCount = history.filter((item) => item.status === "失败").length;
  const pendingCount = taskQueue.filter((item) => item.status === "待开始").length;

  const queueItems = useMemo(
    () =>
      taskQueue.map((item) => ({
        ...item,
        isSelected: item.id === selectedTaskId,
        isActive: item.id === activeTaskId && running,
      })),
    [activeTaskId, running, selectedTaskId, taskQueue],
  );

  useEffect(() => {
    void (async () => {
      try {
        const [saved, savedHistory] = await Promise.all([
          invoke<Partial<AppState> | null>("load_settings"),
          invoke<HistoryEntry[]>("load_history"),
        ]);
        setHistory(savedHistory);
        if (saved) {
          setForm((prev) => ({ ...prev, ...saved }));
        }
      } catch {
        // ignore
      }
    })();
  }, []);

  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | undefined;

    void (async () => {
      unlisten = await listen<ProgressPayload>("transcription-log", (event) => {
        if (!mounted) return;
        const line = event.payload.line.trim();
        if (!line) return;
        setLiveLog((prev) => [...prev, line]);
        if (activeTaskId) {
          setTaskQueue((prev) =>
            prev.map((task) => {
              if (task.id !== activeTaskId) return task;
              const step = getStepFromLine(line, task.mode);
              const stepLabel = getStatusFromLine(line, task.mode) ?? getStepLabel(task.mode, step ?? 0);
              return {
                ...task,
                log: [...task.log, line],
                progress: step ? Math.min(95, step * 20) : task.progress,
                stepLabel,
              };
            }),
          );
        }
        const nextStatus = getStatusFromLine(line, form.mode);
        if (nextStatus) {
          setStatus(nextStatus);
        }
      });
    })();

    return () => {
      mounted = false;
      if (unlisten) {
        unlisten();
      }
    };
  }, [activeTaskId, form.mode]);

  async function saveSettings() {
    await invoke("save_settings", {
      settings: {
        mode: form.mode,
        modelPath: form.modelPath,
        bucketMinutes: form.bucketMinutes,
        threads: form.threads,
        dashscopeApiKey: form.dashscopeApiKey,
        ossEndpoint: form.ossEndpoint,
        ossBucket: form.ossBucket,
        ossAccessKeyId: form.ossAccessKeyId,
        ossAccessKeySecret: form.ossAccessKeySecret,
      },
    });
    setStatus("配置已保存");
  }

  async function clearSettings() {
    await invoke("clear_settings");
    setForm(defaultState);
    setStatus("配置已清空");
  }

  async function persistHistory(entry: HistoryEntry) {
    await invoke("append_history", { entry });
    setHistory((prev) => [entry, ...prev].slice(0, 50));
  }

  async function clearHistoryEntries() {
    await invoke("clear_history");
    setHistory([]);
    setStatus("历史记录已清空");
  }

  async function revealPath(path: string) {
    if (!path) return;
    await invoke("reveal_in_finder", { path });
  }

  async function copyErrorSummary() {
    if (!errorGuide || !error) return;
    const payload = [
      `错误编号：${errorGuide.code}`,
      `错误名称：${errorGuide.title}`,
      `问题说明：${errorGuide.reason}`,
      `处理建议：${errorGuide.fix}`,
      "原始错误：",
      error,
    ].join("\n");
    await navigator.clipboard.writeText(payload);
    setCopiedError(true);
    window.setTimeout(() => setCopiedError(false), 2000);
  }

  function syncFormFromTask(task: QueueTask) {
    setSelectedTaskId(task.id);
    setForm((prev) => ({
      ...prev,
      mode: task.mode,
      inputPath: task.inputPath,
      outputPath: task.outputPath,
      jsonOutputPath: task.jsonOutputPath,
    }));
  }

  async function openPreviewWindow(task: QueueTask) {
    if (!task.previewRows.length && !task.log.length) {
      setStatus("这条任务还没有可预览内容");
      return;
    }
    localStorage.setItem(
      `${previewStoragePrefix}${task.id}`,
      JSON.stringify({
        title: task.inputPath,
        rows: task.previewRows,
        log: task.log,
      } satisfies PreviewPayload),
    );

    const label = `preview-${task.id}`;
    const webview = new WebviewWindow(label, {
      title: `分钟稿预览 - ${task.inputPath.split("/").pop() ?? "任务"}`,
      url: `/?previewTaskId=${task.id}`,
      width: 1180,
      height: 820,
      resizable: true,
    });

    try {
      await webview.once("tauri://created", () => {});
    } catch {
      // ignore if window already exists
    }
  }

  async function pickInput() {
    try {
      const selected = await open({
        multiple: true,
        filters: [{ name: "Media", extensions: ["mp4", "mov", "m4a", "mp3", "wav", "webm"] }],
      });

      const files = typeof selected === "string" ? [selected] : selected ?? [];
      if (!files.length) return;

      const tasks = files.map((inputPath) => {
        const paths = buildTaskPaths(inputPath, form.mode);
        return {
          id: crypto.randomUUID(),
          mode: form.mode,
          inputPath,
          outputPath: paths.outputPath,
          jsonOutputPath: paths.jsonOutputPath,
          status: "待开始" as const,
          progress: 0,
          stepLabel: "待开始",
          previewRows: [],
          log: [],
        };
      });

      setTaskQueue(tasks);
      syncFormFromTask(tasks[0]);
    } catch (pickError) {
      setError(pickError instanceof Error ? pickError.message : String(pickError));
      setStatus("打开文件选择器失败");
    }
  }

  async function pickModel() {
    try {
      const selected = await open({
        multiple: false,
        filters: [{ name: "Whisper Model", extensions: ["bin"] }],
      });
      if (typeof selected === "string") {
        setForm((prev) => ({ ...prev, modelPath: selected }));
      }
    } catch (pickError) {
      setError(pickError instanceof Error ? pickError.message : String(pickError));
      setStatus("打开模型选择器失败");
    }
  }

  async function pickOutput() {
    try {
      const selected = await save({
        defaultPath: form.outputPath || "直播稿.xlsx",
        filters: [{ name: "Excel", extensions: ["xlsx"] }],
      });
      if (typeof selected === "string") {
        setForm((prev) => ({ ...prev, outputPath: selected }));
        if (selectedTaskId) {
          setTaskQueue((prev) => prev.map((task) => (task.id === selectedTaskId ? { ...task, outputPath: selected } : task)));
        }
      }
    } catch (pickError) {
      setError(pickError instanceof Error ? pickError.message : String(pickError));
      setStatus("打开导出路径选择器失败");
    }
  }

  async function executeTask(task: QueueTask) {
    setSelectedTaskId(task.id);
    setActiveTaskId(task.id);
    setError("");
    setCopiedError(false);
    setLiveLog([]);
    setForm((prev) => ({
      ...prev,
      mode: task.mode,
      inputPath: task.inputPath,
      outputPath: task.outputPath,
      jsonOutputPath: task.jsonOutputPath,
    }));
    setTaskQueue((prev) =>
      prev.map((item) =>
        item.id === task.id
          ? {
              ...item,
              status: "处理中",
              progress: 0,
              stepLabel: "准备开始",
              log: [],
            }
          : item,
      ),
    );
    setStatus(task.mode === "aliyun" ? "正在准备阿里云转写..." : "正在准备本地转写...");

    const result =
      task.mode === "aliyun"
        ? await invoke<RunResponse>("run_aliyun_transcription", {
            request: {
              inputPath: task.inputPath,
              outputPath: task.outputPath,
              jsonOutputPath: task.jsonOutputPath,
              bucketMinutes: form.bucketMinutes,
              dashscopeApiKey: form.dashscopeApiKey,
              ossEndpoint: form.ossEndpoint,
              ossBucket: form.ossBucket,
              ossAccessKeyId: form.ossAccessKeyId,
              ossAccessKeySecret: form.ossAccessKeySecret,
            },
          })
        : await invoke<RunResponse>("run_local_transcription", {
            request: {
              inputPath: task.inputPath,
              modelPath: form.modelPath,
              outputPath: task.outputPath,
              bucketMinutes: form.bucketMinutes,
              threads: form.threads,
            },
          });

    setTaskQueue((prev) =>
      prev.map((item) =>
        item.id === task.id
          ? {
              ...item,
              status: "已完成",
              progress: 100,
              stepLabel: "导出 Excel",
              outputPath: result.outputPath,
              previewRows: result.rows,
              log: result.log,
            }
          : item,
      ),
    );
    setStatus(`已完成，共 ${result.rows.length} 条分段`);
    await persistHistory({
      id: crypto.randomUUID(),
      createdAt: new Date().toISOString(),
      mode: task.mode,
      inputPath: task.inputPath,
      outputPath: result.outputPath,
      status: "已完成",
      rowsCount: result.rows.length,
      errorCode: null,
      errorMessage: null,
    });
  }

  async function runTranscription() {
    const startIndex = selectedTaskId ? taskQueue.findIndex((task) => task.id === selectedTaskId) : 0;
    const queueToRun = taskQueue.slice(startIndex >= 0 ? startIndex : 0).filter((task) => task.status === "待开始" || task.status === "失败");

    if (!queueToRun.length) {
      setStatus("没有可处理的任务");
      return;
    }

    setRunning(true);
    let success = 0;
    let failed = 0;

    for (const task of queueToRun) {
      try {
        await executeTask(task);
        success += 1;
      } catch (invokeError) {
        const message = invokeError instanceof Error ? invokeError.message : String(invokeError);
        const guide = getErrorGuide(message);
        setError(message);
        setTaskQueue((prev) =>
          prev.map((item) =>
            item.id === task.id
              ? {
                  ...item,
                  status: "失败",
                  progress: 100,
                  stepLabel: `错误 ${guide.code}`,
                }
              : item,
          ),
        );
        await persistHistory({
          id: crypto.randomUUID(),
          createdAt: new Date().toISOString(),
          mode: task.mode,
          inputPath: task.inputPath,
          outputPath: task.outputPath,
          status: "失败",
          rowsCount: 0,
          errorCode: guide.code,
          errorMessage: message,
        });
        failed += 1;
      }
    }

    setRunning(false);
    setActiveTaskId("");
    setStatus(`队列处理完成，成功 ${success} 条，失败 ${failed} 条`);
  }

  const aliyunDisabled =
    !taskQueue.length ||
    !form.dashscopeApiKey ||
    !form.ossEndpoint ||
    !form.ossBucket ||
    !form.ossAccessKeyId ||
    !form.ossAccessKeySecret;
  const localDisabled = !taskQueue.length || !form.modelPath;
  const disabled = running || (form.mode === "aliyun" ? aliyunDisabled : localDisabled);

  return (
    <div className="app-shell">
      <section className="hero">
        <div>
          <p className="eyebrow">Desktop Workflow</p>
          <h1>直播稿助手</h1>
          <p className="subcopy">自动上传 OSS、调用百炼转写、按分钟聚合、导出 Excel。</p>
        </div>
        <div className="status-card">
          <span>当前状态</span>
          <strong>{status}</strong>
          {running ? <p className="status-hint">当前正在按任务队列顺序执行，预览请点任务里的“查看分钟稿”。</p> : null}
          {!running ? <p className="status-hint">历史记录支持直接打开 Finder 查看上次导出文件夹。</p> : null}
        </div>
      </section>

      <section className="summary-strip">
        <div className="summary-card">
          <span>当前选中</span>
          <strong>{currentTask ? currentTask.inputPath.split("/").pop() : "未选择"}</strong>
        </div>
        <div className="summary-card">
          <span>待处理</span>
          <strong>{pendingCount}</strong>
        </div>
        <div className="summary-card">
          <span>历史成功</span>
          <strong>{successCount}</strong>
        </div>
        <div className="summary-card">
          <span>历史失败</span>
          <strong>{failedCount}</strong>
        </div>
      </section>

      {errorGuide ? (
        <div className="modal-backdrop">
          <section className="panel error-modal">
            <div className="panel-header">
              <h2>错误说明</h2>
              <span>{errorGuide.code}</span>
            </div>
            <div className="guide-grid">
              <div>
                <strong>{errorGuide.title}</strong>
                <p>{errorGuide.reason}</p>
              </div>
              <div>
                <strong>你可以这样处理</strong>
                <p>{errorGuide.fix}</p>
              </div>
            </div>
            <div className="raw-error">
              <strong>原始错误详情</strong>
              <pre>{error}</pre>
            </div>
            <div className="modal-actions">
              <button className="secondary-button" type="button" onClick={copyErrorSummary}>
                {copiedError ? "已复制" : "一键复制报错信息"}
              </button>
              <button className="primary-button" type="button" onClick={() => setError("")}>
                我知道了
              </button>
            </div>
          </section>
        </div>
      ) : null}

      <section className="panel-grid">
        <div className="panel form-panel">
          <div className="field">
            <label>运行模式</label>
            <select value={form.mode} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, mode: event.target.value as Mode }))}>
              <option value="aliyun">阿里云自动上传转写</option>
              <option value="local">本地 Whisper 转写</option>
            </select>
          </div>

          <div className="field">
            <label>视频文件</label>
            <div className="inline-picker">
              <input value={form.inputPath} readOnly placeholder="选择一个或多个视频文件" />
              <button type="button" disabled={running} onClick={pickInput}>
                批量选择
              </button>
            </div>
          </div>

          <div className="field">
            <label>导出路径</label>
            <div className="inline-picker">
              <input value={form.outputPath} readOnly placeholder="选择当前任务的 Excel 输出路径" />
              <button type="button" disabled={running || !selectedTaskId} onClick={pickOutput}>
                选择
              </button>
            </div>
          </div>

          {form.mode === "aliyun" ? (
            <>
              <p className="helper-text">阿里云模式会自动上传 OSS 并提交百炼，不需要你自己上传公网文件。</p>
              <div className="field">
                <label>百炼 API 密钥</label>
                <input type="password" value={form.dashscopeApiKey} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, dashscopeApiKey: event.target.value }))} placeholder="填写阿里云百炼的 API Key，例如 sk-..." />
              </div>
              <div className="field-row">
                <div className="field">
                  <label>OSS 地域地址</label>
                  <input value={form.ossEndpoint} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, ossEndpoint: event.target.value }))} placeholder="例如 oss-cn-beijing.aliyuncs.com" />
                </div>
                <div className="field">
                  <label>OSS 存储桶名称</label>
                  <input value={form.ossBucket} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, ossBucket: event.target.value }))} placeholder="例如 zhibogao" />
                </div>
              </div>
              <div className="field">
                <label>阿里云存储账号 ID</label>
                <input value={form.ossAccessKeyId} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, ossAccessKeyId: event.target.value }))} placeholder="填写阿里云 OSS 的 AccessKey ID" />
              </div>
              <div className="field">
                <label>阿里云存储账号密钥</label>
                <input type="password" value={form.ossAccessKeySecret} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, ossAccessKeySecret: event.target.value }))} placeholder="填写阿里云 OSS 的 AccessKey Secret" />
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label>模型文件</label>
                <div className="inline-picker">
                  <input value={form.modelPath} readOnly placeholder="选择 ggml 模型" />
                  <button type="button" disabled={running} onClick={pickModel}>
                    选择
                  </button>
                </div>
              </div>
              <div className="field">
                <label>线程数</label>
                <input type="number" min={1} max={12} value={form.threads} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, threads: Number(event.target.value) }))} />
              </div>
            </>
          )}

          <div className="field">
            <label>分钟粒度</label>
            <select value={form.bucketMinutes} disabled={running} onChange={(event) => setForm((prev) => ({ ...prev, bucketMinutes: Number(event.target.value) }))}>
              <option value={1}>1 分钟</option>
              <option value={3}>3 分钟</option>
              <option value={5}>5 分钟</option>
            </select>
          </div>

          <button className="primary-button" type="button" disabled={disabled} onClick={runTranscription}>
            {running ? "队列处理中..." : "开始转写"}
          </button>

          <div className="secondary-actions">
            <button className="secondary-button" type="button" disabled={running} onClick={saveSettings}>
              保存配置
            </button>
            <button className="secondary-button" type="button" disabled={!history.length || running} onClick={clearHistoryEntries}>
              清空历史
            </button>
          </div>

          <button className="ghost-button" type="button" disabled={running} onClick={clearSettings}>
            清空配置
          </button>
        </div>

        <div className="panel queue-panel">
          <div className="panel-header">
            <h2>任务队列</h2>
            <span>{queueItems.length ? `共 ${queueItems.length} 条` : "还没有任务"}</span>
          </div>
          <div className="queue-list compact">
            {queueItems.length === 0 ? (
              <p className="empty-cell">先点“批量选择”导入视频，这里会显示每条任务的状态和进度。</p>
            ) : (
              queueItems.map((item) => (
                <div className={`queue-row compact ${item.isSelected ? "current" : ""}`} key={item.id}>
                  <button className="queue-select" type="button" onClick={() => syncFormFromTask(item)}>
                    <div className="queue-head">
                      <strong>{item.inputPath.split("/").pop() ?? item.inputPath}</strong>
                      <span className={`queue-status ${item.status}`}>{item.status}</span>
                    </div>
                    <p className="queue-path">{item.inputPath}</p>
                    <div className="queue-progress">
                      <div className="progress-bar">
                        <div className={`progress-fill ${item.isActive ? "running" : ""}`} style={{ width: `${item.progress}%` }} />
                      </div>
                      <span>{item.progress}%</span>
                    </div>
                    <p className="queue-meta">当前步骤：{item.stepLabel}</p>
                  </button>
                  <div className="queue-actions">
                    <button className="secondary-button" type="button" disabled={!item.previewRows.length && !item.log.length} onClick={() => void openPreviewWindow(item)}>
                      查看分钟稿
                    </button>
                    <button className="ghost-button" type="button" disabled={!item.outputPath} onClick={() => void revealPath(item.outputPath)}>
                      打开文件夹
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
          <div className="inline-log">
            <div className="panel-header">
              <h2>当前日志</h2>
              <span>{liveLog.length} 条</span>
            </div>
            <div className="log-list compact-log">
              {liveLog.length ? liveLog.map((entry, index) => <code key={`${entry}-${index}`}>{entry}</code>) : <p className="empty-cell">处理开始后，这里只显示当前正在执行任务的实时日志。</p>}
            </div>
          </div>
        </div>
      </section>

      <section className="panel history-panel">
        <div className="panel-header">
          <h2>历史记录</h2>
          <span>{history.length ? `最近 ${history.length} 条` : "还没有记录"}</span>
        </div>
        <div className="history-list">
          {history.length === 0 ? (
            <p className="empty-cell">处理完成后，这里会保留记录，并支持直接打开 Finder。</p>
          ) : (
            history.map((item) => (
              <div className="history-card" key={item.id}>
                <div className="history-head">
                  <strong>{item.inputPath.split("/").pop() ?? item.inputPath}</strong>
                  <span className={`queue-status ${item.status}`}>{item.status}</span>
                </div>
                <p className="history-meta">{formatHistoryTime(item.createdAt)} | {item.mode === "aliyun" ? "阿里云自动上传转写" : "本地 Whisper 转写"}</p>
                <p className="history-meta">导出：{item.outputPath || "未生成"}</p>
                {item.errorCode ? <p className="history-error">错误编号：{item.errorCode}</p> : null}
                <div className="history-actions">
                  <button className="secondary-button" type="button" disabled={!item.outputPath} onClick={() => void revealPath(item.outputPath)}>
                    在 Finder 中打开
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="panel guide-panel">
        <div className="panel-header">
          <h2>填写帮助</h2>
          <span>{history.length ? `最近一次：${formatHistoryTime(history[0].createdAt)}` : "新手版"}</span>
        </div>
        <div className="guide-grid">
          <div>
            <strong>百炼 API 密钥填什么</strong>
            <p>填阿里云百炼控制台里生成的 `sk-...`，就是你之前发给我的那类密钥。</p>
          </div>
          <div>
            <strong>OSS 地域地址填什么</strong>
            <p>填完整域名，例如 `oss-cn-beijing.aliyuncs.com`。</p>
          </div>
          <div>
            <strong>历史记录怎么用</strong>
            <p>历史记录主要用来回看处理结果，并直接打开 Finder 找到导出的文件夹。</p>
          </div>
          <div>
            <strong>分钟稿预览在哪里看</strong>
            <p>在任务队列里点“查看分钟稿”，会打开独立窗口，不会挤占主界面空间。</p>
          </div>
        </div>
      </section>
    </div>
  );
}
