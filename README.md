# Contact Lab：独立的双侧接触标注页面

代码独立放在 `/home/liushiqi/Annotation`，直接读取 LeRobot 原始文件，不依赖 Cleaner 服务。数据集只读，标注保存在单独的 SQLite 数据库，支持导出 JSON。

默认流程：曲线圈定接触／离开候选段 → 模型检查候选段的连续帧 → 网页重点复核 → 人工确定左右侧开始／结束帧。也保留手动检查单帧／小范围的功能。

## 网页默认曲线优先

更新部署后，筛查面板标题为“曲线优先一键筛查”。选择“先试当前 EP（左右侧）”，点击“开始曲线 → 模型筛查”测试；满意后选择“全部 EP（左右侧）”创建完整任务。两者均先完成曲线圈段，再调用模型。窗口总数只计算候选段，同时显示全帧窗口数作为对照、处理进度及预计剩余时间。每侧完成后，候选段自动进入“重点人工复核”，展示曲线括区和模型建议边界，可直接跳转并逐帧标记。平坦／缺失曲线保留整侧警告，不自动推断全程无接触。

这是新的网页默认方式，无需每次运行脚本。旧全帧任务仍可继续，但按钮明确写“继续旧全帧筛查（较慢）”；它不会自动转换为曲线优先任务，测试新流程请点“开始”。标注和历史进度保留，不自动启动模型任务。

GPU 容器运行的是提交时的源代码副本，本地修改不会自动生效。先 `bash submit.sh --validate-only`；确认当前实验 ID 后，在集群界面取消旧服务实验、关闭旧转发，再执行 `bash submit.sh --publish` 上传新版本，避免重复占用 GPU。笔记本 SSH 转发目标仍为远程 8088。网页可用后强制刷新；`curl --noproxy '*' http://127.0.0.1:8088/api/health` 返回 `screening_pipeline: "curve_first"` 才是新版后端。当前执行环境无法连接 master，尚未代为部署或验证新 GPU 筛查。

## 曲线先圈段，再交给模型

`curve_first.py` 是独立命令行脚本：先扫描所有 EP 左右侧的载荷／形变曲线，用相对双阈值和连续两点确认，分别圈定可能的首次接触、首次离开；保留短暂尖峰、片头已接触和片尾仍接触的候选。每个边界段默认前后各扩展 0.35 秒，多条曲线的重叠段合并，再将段内连续触觉帧按每窗 6 帧、重叠 1 帧交给现有网页模型服务。不会加载第二份模型，也不会改写人工标注。

先停止网页中的全量筛查，等待其状态变为已停止。保持远程的 `submit.sh --publish` 或 `attach.py` 转发运行；在**远程开发环境终端**执行以下命令，使用远程 8088，笔记本浏览器仍使用原来的 SSH 地址。只生成曲线计划、不调用模型：

```bash
cd /home/liushiqi/Annotation
/home/liushiqi/venvs/anytouch2/bin/python curve_first.py \
  --dataset /run/determined/NAS1/public/xihang/data-mechanism/r2s2r-runs/chip_cradle_v4/prod100_s1000_e65263/export_a6135f6f/xarm7 \
  --plan-only --output state/curve_first_plan.json
```

该数据集的 48 个 EP 实测圈出 253 段、3,292 个模型窗口，是全帧 29,710 窗口的 11.1%；这是规划结果，尚未执行这些窗口的 GPU 推理。已生成上述计划时无需再次规划。检查计划后，运行以下命令开始模型筛查，按实际目标修改补充说明：

```bash
/home/liushiqi/venvs/anytouch2/bin/python curve_first.py \
  --resume --output state/curve_first_plan.json \
  --url http://127.0.0.1:8088 \
  --prompt '只判断本侧夹爪接触面与操作目标物体的物理接触，不把接触桌面算进去。'
```

终端会显示当前 EP／侧／时间段、完成窗口数及剩余时间（至少完成两窗后，按最近 20 窗端到端耗时估计）。Ctrl+C 停止提交后续窗口；当前窗口可能仍会完成。用同一命令继续会复用已保存的任务 ID 并跳过完成窗口，须保留相同的 `--prompt` 和 `--camera`。当前窗口的任务 ID 若因服务重启或历史清理而失效，脚本会报错并保留进度，不自动当作成功。

计划和结果保存在同一个 JSON：`units[].regions[].boundaries` 是曲线候选括区及扩展范围，`model_review.boundaries` 是模型建议的边界两帧和候选时间，`tasks[].result` 保留原始模型输出。所有候选均待人工确认；无效／遗漏／冲突的输出保留为不确定，连续三窗无效时停止。平坦、缺失及异常曲线列入 `units[].warnings`，不会推断为全程无接触。未查看的稳定区段不代表模型已确认无接触，自动圈段可能漏掉曲线无响应的接触。

脚本输出是独立报告，不会自动导入网页复核队列；新版网页直接启动的曲线优先任务会自动进入网页队列。按独立报告里的 EP、侧和时间段在网页逐帧复核，然后保存人工开始／结束帧。脚本可用 `--episodes 0,1`、`--side left` 先规划小范围，或用 `--padding`、`--on-ratio`、`--off-ratio` 调整圈段参数；新计划请指定新的 `--output`。

## 全数据集筛查与人工选帧

1. 打开数据集，在右侧“补充说明”描述目标，例如“只判断夹爪与黑色插头的接触，不包括桌面”。选择场景相机。
2. 选择“先试当前 EP”或“全部 EP”，点击“开始曲线 → 模型筛查”。默认先用曲线圈出候选段，仅将段内本侧原始触觉采样逐帧送入模型；每窗 6 个连续时间点，窗口重叠 1 帧，窗口不跨越两个候选段之间未查看的空白。每个时间点同时送入场景与本侧触觉图像。缺少原始触觉时钟会退回数据帧并生成重点警告。
3. 页面显示处理窗口、侧/EP 进度和当前区段。处理至少两个窗口后，根据最近 20 个窗口的端到端耗时估算剩余时间；包含读图和推理，首次加载、GPU 负载、曲线处理会影响估算，不包含人工复核时间。
4. 系统自动使用本侧载荷／形变曲线圈段。基线为低分位值，考虑基线噪声，用较低的接触／释放双阈值寻找变化起点并连续 2 点确认；默认边界段前后各扩展 0.35 秒。仿真 `in_contact` 不作为独立传感器。自动阈值不是物理接触标定，平坦、缺失、异常曲线保留警告。
5. 模型不确定／意见冲突、缺失输入、推理错误或段内未找到可靠变化会显示为橙色重点区域；其他候选段也保留人工边界选帧。未筛片段不会被推断成模型已确认无接触，曲线无响应的接触可能漏检。
6. 点“查看 / 逐帧定位”，自动切换到本侧触觉时钟；用 ← / → 找第一帧接触／第一帧未接触。点“新建左/右侧区间”，分别用“设为当前帧”标记开始和结束，勾选“人工已确认”后保存。首尾接触可用“开头已经接触／结束仍在接触”。无接触可勾选本侧“全程无接触”。然后点击该疑点的“此处已人工复核”；误报也可以保存空标注后记录复核。

筛查任务、逐窗进度、自动结果和复核记录持久保存在状态 SQLite 中；不会覆盖人工区间。停止后可继续，已处理窗口不重复推理。服务重启会将未完成任务标为中断，需手动继续。连续三个模型窗口失败会停止，检查错误后可继续；失败窗口仍保留为不确定，不能当作无接触。推理期间手动模型任务与全量任务互斥，人工查看及保存仍可使用。复核记录绑定当前保存版本，修改标注后同 EP 已复核项会重新变成待复核。导出附带最近一次筛查和复核状态。

历史实验 65399 曾确认健康接口返回 `ok:true`、模型 ready、网页打开；请勿把这个历史 ID 当作当前实验。新的曲线优先网页批处理尚未在 GPU 上验收；更新步骤见上面的“网页默认曲线优先”。

## 提交到 Determined

### 65398 的代理端口问题（已定位）

实验 65398 / Trial 66777 已在 A100 上完成真实的两图模型推理，日志包含 `MODEL_SMOKE_OK`。但用户返回的诊断结果中 `environment.proxy_ports` 和运行 allocation 的 `proxyPorts` 均为空，因此该实验没有注册 8088 代理，访问 `/proxy/<task-id>:8088/` 返回 404。

两个部署 YAML 已补充 `environment.proxy_ports: [{proxy_port: 8088, proxy_tcp: true}]`。master 的实际校验反馈要求 YAML 字段使用 `proxy_port`，不能照搬 API 返回对象中的 `port` 字段。这与 `environment.ports` 的容器端口声明不同；本地改动不会修改正在运行的实验。先让 master 校验配置（不创建实验、不调度 GPU）：

```bash
bash submit.sh --validate-only
```

只有返回 `CONFIG_VALIDATED` 后，再停止旧转发、取消旧实验并提交替代实验，避免重复占用 GPU。`submit.sh` 检测到 `.venv-det/bin/det` 时优先使用该独立客户端。用户已用 65399 验证代理登记、`/api/health` 成功和网页打开。下面较早的故障记录用于保留排查背景。

已配置 `amp-48g` 资源池、单 GPU、统一镜像 `10.130.130.5:8192/lab-dev:latest`，未设置 `agentUser`。Fairshare 是调度策略；实际启动时间和 GPU 型号由资源池分配，76.47% 是用户提供的当时界面值。

模型直接从以下本地目录加载，不下载模型权重：

```text
/run/determined/NAS1/public/HuggingFace/Qwen/Qwen3-VL-8B-Instruct
```

在可访问集群的开发容器中运行：

```bash
cd /home/liushiqi/Annotation
# 仅首次或登录过期时需要：
det -m http://10.130.130.5:4096 user login liushiqi
bash submit.sh --publish
```

该命令上传源代码，创建 `experiment.yaml` 中的实验，并通过 Determined CLI 将任务的 8088 端口转发到当前机器的 8088；保持终端打开。服务启动后在这台机器打开 `http://127.0.0.1:8088/`。如果开发容器在远程主机上，再把该主机的 8088 端口通过 SSH 或编辑器转发至笔记本。

`bash submit.sh` 只提交实验。`bash submit.sh --command` 使用长驻 Command 任务类型提交，配置在 `command.yaml`。只选择一种方式，避免重复占用 GPU。长驻服务需要结束时手动取消实验或终止 Command。

CLI 0.22.2 的 `--publish` 必须与 `--follow-first-trial` 一起使用，提交脚本已包含这两个参数。若实验已创建但终端返回、或转发中断，连接现有实验即可，无需重新提交：

```bash
python3 attach.py 65396
```

将 65396 替换为实际实验 ID。脚本使用已安装 Determined CLI 的转发实现及已登录身份，展示任务日志。需要单独检查状态和启动失败原因时运行：

```bash
det -m http://10.130.130.5:4096 e describe 65396
det -m http://10.130.130.5:4096 e logs 65396 --tail 80
```

若新终端读取日志只显示空白错误，或 8088 由 `det` 监听但访问时连接重置，使用更新后的脚本：

```bash
python3 attach.py 65398 --logs-only --tail 60
```

旧版 det 的 WebSocket 库 lomond 不处理 `NO_PROXY`，会继续使用 `HTTP_PROXY`，而普通 API 请求可能已经直连成功。现在 `submit.sh` 和 `attach.py` 都仅在自身进程内清除代理变量，确保内网 master 的 API 和转发均直连，不修改用户终端或全局代理。已有的旧转发进程不会自动更新：在其终端按 Ctrl+C 关闭旧转发（不取消实验），再运行 `python3 attach.py 65398`。实验 ID 应替换为实际运行的任务。随后用 `curl --noproxy '*' --max-time 10 http://127.0.0.1:8088/api/health` 检查。

### 笔记本访问远程页面

master 0.38.1 推荐使用独立的同版本客户端，不修改全局 Python：

```bash
python3 -m venv .venv-det
.venv-det/bin/python -m pip install 'determined==0.38.1'
.venv-det/bin/python attach.py 65398
```

`attach.py` 兼容 0.38.x 的 `login_with_cache` 和旧版 `Authentication`，使用客户端自己的登录缓存。若升级前的转发仍在运行，先在原终端 Ctrl+C，避免占用同一个端口。创建本地监听不代表端到端转发成功；必须通过 `/api/health` 验证。若 WebSocket 仍返回 404，需要检查运行任务的代理服务/端口注册，不能仅归因于模型未加载或笔记本端口。

先让远程开发环境中的 `submit.sh --publish` 或 `attach.py` 保持运行。这一步把 GPU 任务的 8088 转发到开发环境；还需要把开发环境的端口转发到笔记本。

若通过 VS Code Remote SSH / Dev Containers 使用该开发环境，在同一个远程窗口的“端口 / Ports”面板选择“转发端口 / Forward a Port”，输入 `8088`，使用面板显示的本地地址打开。若本机 8088 已占用，面板可能分配另一个本地端口。

若通过 SSH 直接登录运行转发命令的环境，在**笔记本 PowerShell / 终端**执行（使用已配置的 `liushiqi-dev` 别名）：

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8088:127.0.0.1:8088 liushiqi-dev
```

保持该终端打开，再访问 `http://127.0.0.1:8088/`。SSH 会使用笔记本配置中该别名的 HostName、User 和 IdentityFile，无需在命令里再次指定私钥。如果 SSH 只到宿主机而应用转发在未发布端口的开发容器内，应使用编辑器对该容器的端口转发功能，或先配置容器端口映射。

### 容器创建失败：不存在的挂载源

实验 65396 / Trial 66775 的日志显示 GPU 已分配，但容器因宿主机不存在 `/mount/ccai_nas2/shuai/determined` 而创建失败，未执行 `start.sh`。此时端口转发无法使页面可用。

已通过实际实验配置确认，该路径来自 master 默认的 `checkpoint_storage.host_path`，`bind_mounts` 为空。可用下面的命令检查某个实验的有效配置：

```bash
python3 inspect_mounts.py 65396
```

这个只读脚本通过已登录身份读取实际实验配置，使用 JSON 输出，仅显示存储/挂载字段和报错路径在配置中的位置。它绕过当前 CLI 0.22.2 调用已被新版 ruamel.yaml 移除的 `safe_dump()` 所导致的报错，不修改全局 Python 依赖或集群配置。

`experiment.yaml` 现已显式设置 `checkpoint_storage.type: shared_fs`、`host_path: /mount/HOME/liushiqi` 和 `storage_path: Annotation/determined-checkpoints`，覆盖失效的默认路径。宿主机路径根据用户提供的 LDAP home 说明选取；仍需在下一次调度时验证该路径在 amp-48g 节点存在且可访问。没有修改 master 全局配置。

重新运行 `bash submit.sh --publish` 创建使用新配置的实验，并使用新实验 ID 检查日志或恢复端口转发。原实验 65396 已处于 ERROR 状态，不会因本地 YAML 修改自动更新。若新日志仍提示宿主机路径缺失，需要确认 GPU 节点的真实 home 挂载位置；开发容器内创建同名目录不能解决宿主机路径问题。

本地接入 Tailscale 后可设置 `DET_MASTER=http://node1.zjuscl.lab:4096`，但仍须从能读取本项目源代码的目录提交。

任务启动脚本会检查模型目录，创建独立运行环境，安装兼容的 PyTorch 2.8 / torchvision 0.23 / Transformers 4.57.6，检查 ffmpeg、CUDA，再启动网页。优先使用系统 ffmpeg；镜像未安装时使用 imageio-ffmpeg 的二进制。镜像需要能安装 requirements；CUDA、软件安装及 NAS/home 挂载尚待实际集群验证。

标注状态显式保存在运行容器的 `/home/liushiqi/Annotation/state`，由 `/mount/HOME/liushiqi` 的读写挂载持久保存，不依赖容器的 HOME 环境变量。可用 `ANNOTATION_STATE_DIR` 调整。启动脚本先检查模型配置、样例数据和状态目录写权限，成功后打印 `PATHS_OK`，再安装依赖。pip 和 Hugging Face 缓存默认也放在状态目录下。

实验 65397 已在 node2-a6000 / node7-a100-40g 上通过 checkpoint 存储检查，但因模型目录不可读而退出。其有效配置的 `bind_mounts` 为空，说明开发容器中的 NAS 挂载没有通过本项目配置传入任务容器。

现在 `experiment.yaml` 和 `command.yaml` 都显式添加了以下挂载：

| GPU 宿主机目录 | 任务容器目录 | 权限 |
| --- | --- | --- |
| `/mount/NAS1/public` | `/run/determined/NAS1/public` | 只读，模型及数据集 |
| `/mount/HOME/liushiqi` | `/home/liushiqi` | 读写，标注及缓存 |

NAS 源路径是根据现有 NAS1 命名和挂载结构作出的部署假设，尚未在 GPU 节点验证；若 Docker 报该源路径不存在，需要确认集群 NAS1 public 的真实宿主机挂载位置。不能用开发容器内的 `/run/determined/NAS1/public` 直接代替宿主机路径。实验调试阶段设置 `max_restarts: 0`，避免固定配置错误连续重试六次。

## 验证真实模型推理

启动时会加载模型，并对默认样例数据集的场景和左侧触觉图像做一次真实推理。成功日志是 `MODEL_SMOKE_OK`，输出同时写入状态目录下的 `model_smoke.json`。加载/测试失败打印 `MODEL_STARTUP_ERROR`；网页保留人工标注能力。

网页上显示“模型已就绪”只说明模型已加载。也可在端口转发后执行下面的独立检查，验证数据读取、图像返回和一次真实模型响应：

```bash
python3 smoke_client.py --url http://127.0.0.1:8088/
```

只有看到 `MODEL_INFERENCE_OK` 和实际生成文本，才算试跑成功。结果默认写入 `verification/model_response.json`。这验证推理链路，不代表接触识别准确率已经达标。

当前 Codex 执行沙箱连接 master 返回 `[Errno 1] Operation not permitted`。用户已在 65399 确认 NAS、模型推理、代理和网页可用；本次批处理可做本地数据／接口检查，新的 GPU 批量推理仍需在更新部署后验收。

## 标注规则与数据

- 每侧支持多次接触。区间是 `[接触第一帧, 离开第一帧)`，离开帧本身已不接触。可以记录“开头已接触”“结束仍接触”和“全程无接触”。
- 数据帧和左右触觉采样各自保留原始时钟。新数据集场景为 15 Hz、触觉为 60 Hz，页面支持按所选时钟逐帧前进。保存同时记录触觉样本序号、时间和最近的数据帧，避免丢失高频边界。
- 读取 v2/v3 metadata、共享视频分段、原始 Parquet、独立 tactile_streams。展示夹爪状态/动作、可用载荷、接触参考状态和形变幅度 P90。旧数据中直接存于 Parquet 的 mesh_motion_3d 也能生成曲线。
- 新样例包含 synthetic / uncalibrated 触觉信号；载荷、接触参考状态和形变曲线不是经物理标定的接触真值。页面会显示提示，最终标签仍需人工确认。
- 曲线复筛采用接触/释放双阈值及连续 2 个采样点确认，回溯到首次越阈值的采样点；阈值使用各曲线原始单位，没有通用的默认准确阈值。
- 手动模型检查支持单帧或时间窗，每窗 2–12 个采样时间点。默认批处理扫描所有所选 EP 左右侧的曲线，只在候选段内使用连续 6 帧、重叠 1 帧的模型窗口；段间不推断接触状态。旧全帧任务可继续其原有覆盖方式。所有自动结果只生成待确认建议。
- 标注保存有版本冲突检查；同侧已确认区间不能重叠。导出包含草稿/候选及 confirmed 字段，后续统计应只使用已确认记录。

默认数据允许根目录为 `/home/liushiqi/datasets`、`/home/liushiqi/dataset_exports`、`/run/determined/NAS1/public`，可用 `DATASET_ALLOWED_ROOTS` 以冒号分隔调整。页面不包含独立账号系统，集群访问应使用 Determined 的访问控制或本机端口转发。

## 本机人工预览与开发检查

此机器现有环境可运行人工页面；其 Transformers 较旧且当前无可用 CUDA，不能运行 Qwen3-VL：

```bash
cd /home/liushiqi/Annotation
/home/liushiqi/venvs/anytouch2/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8088
```

按 ← / → 前后移动，空格播放或暂停。保存前可以调整候选，人工确认后勾选“人工已确认”。

```bash
/home/liushiqi/venvs/anytouch2/bin/python -m unittest -v test_annotation
/home/liushiqi/venvs/anytouch2/bin/python -m unittest -v test_batch
/home/liushiqi/venvs/anytouch2/bin/python -m unittest -v test_curve_first
# 加上真实数据检查：
REAL_DATASET=/path/to/xarm7 /home/liushiqi/venvs/anytouch2/bin/python -m unittest -v test_annotation
node --check static/app.js
node --check static/batch.js
node test_batch_ui.js
bash -n start.sh submit.sh
```

本次本地通过 45 项标注／批处理／曲线优先脚本检查（1 项图像集成检查未在本次重复运行）、6 项 CLI 兼容检查，以及前端状态／动作检查和 JS／Shell 语法检查。新增检查涵盖仅候选段推理、窗口不跨未筛区段、未筛区段不推断无接触、停止／继续不重复推理、缺失曲线不调用模型、当前 EP 测试和默认请求方式。真实 48 EP 的网页批处理规划与独立脚本一致：96 侧/EP、253 段、3,292 / 29,710 窗口，没有调用 GPU。既有图像集成检查曾覆盖 Episode 0、1、20 的 4 路图像首尾帧和原始时钟。前端检查不等同于真实浏览器交互验收；新的候选段 GPU 推理及识别准确性仍待部署后验收。
