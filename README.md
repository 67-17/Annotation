# Contact Lab · 双侧接触标注

同步查看场景相机和 photon 触觉图像，先用曲线圈定接触／离开候选段，再让本地 Qwen3-VL 筛查，最后人工逐帧确认左右夹爪的边界。

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

在项目根目录执行：

```bash
bash submit.sh --validate-only  # 仅校验配置
bash submit.sh --publish        # 提交实验并转发到远程开发环境的 8088
# 对已运行实验建立转发：将 <实验ID> 替换为实际数字
.venv-det/bin/python scripts/attach.py <实验ID>
curl --noproxy '*' --max-time 15 http://127.0.0.1:8088/api/health
```

保持转发终端打开；笔记本再通过 SSH 转发远程开发环境的 8088。完整命令与操作步骤见 [使用和部署指南](docs/guide.md)。

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
