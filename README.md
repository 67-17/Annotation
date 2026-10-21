# Contact Lab · 双侧接触标注

同步查看场景相机和 photon 触觉图像，先用曲线圈定接触／离开候选段，再让本地 Qwen3-VL 筛查，最后人工逐帧确认左右夹爪的边界。

标注区提供“单段／多段”两种模式。单段模式保持每侧一个接触区间；多段模式用于拧紧、松开、回位后再次接触等重复动作，每次接触和离开保存为独立区间。同一数据集保存一次模式后，后续未标注 Episode 会自动沿用。

## 文件目录

| 目录 | 内容 |
| --- | --- |
| `backend/` | Web API、数据读取、模型服务、曲线与批量筛查 |
| `static/` | 网页、样式和前端交互 |
| `scripts/` | 启动、提交、端口转发、挂载诊断和冒烟检查 |
| `configs/` | Determined 实验与 Command 配置 |
| `tests/` | 后端、客户端与前端回归测试 |
| `docs/` | 使用、部署和故障排查说明 |
| `state/` | 标注数据库、筛查进度与缓存（不入 Git） |
| `determined-checkpoints/` | 实验检查点（不入 Git） |

根目录 `app.py`、`start.sh`、`submit.sh` 为兼容入口。虚拟环境 `.venv-det/` 保留原位。

## 启动与访问

以下命令在可访问 Determined 集群的远程开发环境执行。重开实验：

```bash
cd /home/liushiqi/Annotation
# 仅首次或登录过期时需要
det -m http://10.130.130.5:4096 user login liushiqi
bash submit.sh --validate-only
bash submit.sh --publish
```

最后一个命令会创建新实验，并把任务的 8088 转发到远程开发环境的
8088。保持终端打开。继续已有实验只需重新建立转发，不要再次提交：

```bash
cd /home/liushiqi/Annotation
.venv-det/bin/python scripts/attach.py <实验ID>
curl --noproxy '*' --max-time 15 http://127.0.0.1:8088/api/health
```

若远程 8088 已被占用，可使用
`.venv-det/bin/python scripts/attach.py <实验ID> --port 18088`。

笔记本 Windows PowerShell 可以后台建立第二段转发，避免前台 `ssh -N`
阻塞后续命令：

```powershell
$AnnotationSsh = Start-Process ssh -ArgumentList @(
  '-N',
  '-o', 'ExitOnForwardFailure=yes',
  '-o', 'ServerAliveInterval=30',
  '-L', '127.0.0.1:8088:127.0.0.1:8088',
  'liushiqi-dev'
) -PassThru
```

浏览器打开 `http://127.0.0.1:8088/`。本机 8088 被占用时，将 `-L`
参数改为 `127.0.0.1:18088:127.0.0.1:8088`，然后访问
`http://127.0.0.1:18088/`。用 `Stop-Process -Id $AnnotationSsh.Id`
关闭后台转发。完整操作与故障排查见 [使用和部署指南](docs/guide.md)。

## 同时筛查两个数据集

可以同时运行，但必须创建两个 Determined 实验；每个实验各申请一张 GPU。
单个服务同一时刻只允许一个全量模型筛查。两个实验不能使用默认的同一状态目录，
否则会共享 SQLite 筛查状态，并可能把另一个实验的运行任务标记为中断。

分别在远程开发环境的两个终端执行：

```bash
# 终端 A
cd /home/liushiqi/Annotation
ANNOTATION_INSTANCE=dataset-a ANNOTATION_LOCAL_PORT=8088 bash submit.sh --publish
```

```bash
# 终端 B
cd /home/liushiqi/Annotation
ANNOTATION_INSTANCE=dataset-b ANNOTATION_LOCAL_PORT=18088 bash submit.sh --publish
```

`ANNOTATION_INSTANCE` 只能包含字母、数字、下划线和连字符。上面的命令分别使用
`state-dataset-a` 和 `state-dataset-b`，因此标注、筛查进度和 reload token 完全隔离。
两个网页分别在远程开发环境的 8088、18088 端口；在每个网页中导入对应的数据集并开始筛查。

Windows 上再开两个 SSH 后台转发。若本机使用相同端口：

```powershell
$AnnotationSshA = Start-Process ssh -ArgumentList @('-N','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-L','127.0.0.1:8088:127.0.0.1:8088','liushiqi-dev') -PassThru
$AnnotationSshB = Start-Process ssh -ArgumentList @('-N','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-L','127.0.0.1:18088:127.0.0.1:18088','liushiqi-dev') -PassThru
```

分别访问 `http://127.0.0.1:8088/` 和 `http://127.0.0.1:18088/`。
继续两个已有实验时，不要重新提交，分别运行：

```bash
.venv-det/bin/python scripts/attach.py <实验A_ID> --port 8088
.venv-det/bin/python scripts/attach.py <实验B_ID> --port 18088
```

更新代码后也要分别重载两个实例，并确保各自没有正在运行的筛查任务：

```bash
ANNOTATION_STATE_DIR=/home/liushiqi/Annotation/state-dataset-a ANNOTATION_URL=http://127.0.0.1:8088 bash scripts/reload.sh
ANNOTATION_STATE_DIR=/home/liushiqi/Annotation/state-dataset-b ANNOTATION_URL=http://127.0.0.1:18088 bash scripts/reload.sh
```

两个实验能否同时获得 GPU 取决于 `amp-48g` 资源池的空闲容量和 fairshare；排队不代表命令失败。

部署一次支持手动重载的版本后，普通前端／后端代码更新不必创建新实验。保持
现有 `attach.py` 转发，在筛查和模型任务空闲时执行：

```bash
bash scripts/reload.sh
```

服务会从共享项目目录重新读取代码，标注与筛查进度保留。等待日志再次出现
`MODEL_SMOKE_OK` 后刷新网页。依赖、启动脚本或实验配置变化仍需重新部署。

曲线独立脚本改为模块启动：`python -m backend.curve_first --help`。诊断工具在 `scripts/`，例如 `python scripts/inspect_mounts.py <实验ID>`。

## 开发检查

使用已安装项目依赖的 Python，在项目根目录运行：

```bash
python -m unittest tests.test_annotation tests.test_batch tests.test_curve_first tests.test_deployment
.venv-det/bin/python -m unittest tests.test_attach
node tests/test_review_ui.js
node tests/test_batch_ui.js
node tests/test_async_ui.js
bash -n start.sh submit.sh scripts/start.sh scripts/submit.sh scripts/reload.sh
```

首次启用手动重载仍需部署一次；此后普通源文件变更可用 `scripts/reload.sh` 应用。标注数据库与筛查历史保留原路径。
