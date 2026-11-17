# 设计评审渐进渲染服务（预览争用内核）

用户调整相机或材质时先得到**真实的低预算路径追踪预览**，配置稳定后再进入
**最终出图队列**。后台已有长任务时，交互请求仍获得**有限且可预测**的响应：
固定大小的渲染进程池 + 固定数量的累加器槽位，资源不足时显式排队/返回限额，
而不是无限启动进程制造表面快速。

全部基于 **Python 3 标准库**实现（`multiprocessing` + `http.server` +
手写 PNG/zlib 编码），无第三方依赖。

## 它真正交付了什么（不只是任务列表加优先级字段）

| 需求 | 实现位置 | 机制 |
|---|---|---|
| 可计量的计算片段 | `engine/kernel.py` | 每个作业切成 32×32 tile，每片固定 1spp（预览）/4spp（最终），分片是调度与预算的最小单位 |
| 优先级 + 等待时间调度 | `Kernel._score` | 分数 = 优先级 + 老化项；最终图基础分更高 |
| 防最终图饿死 | `Kernel._score` | 最终图等待超过 6 秒获得 +1000 跃升，连续预览无法永久阻塞它 |
| 完整配置版本绑定 | `pathtracer.config_hash` | 每个作业冻结 scene+camera+材质+尺寸+spp，绑定 12 位 sha256 |
| 旧请求晚到不覆盖当前视图 | 内核取代 + 前端 stale-gate | 新相机/材质版本取代同评审者旧预览；前端按 `X-Cfg-Hash` 拒绝绘制过期帧 |
| 过时预览停止消耗预算 | `Kernel._cancel_locked` + worker 行级取消检查 | 取消事件置位，在途 tile 在下一行即停，槽位回收 |
| 资源不足明确排队/限额 | 槽位池 + `REVIEWER_QUOTA` + `GLOBAL_QUEUE_LIMIT` | HTTP 429 带原因；槽位满则排队，不新建进程 |
| 失败反馈回预览争用 | `_worker` 捕获 + `_on_chunk` | 渲染异常变成作业 `failed` 并释放槽位，错误显示在面板 |
| 真实逐步改善的图像 | `snapshot_png` 每分片刷新 | 均值随有效采样数提升，颗粒感逐帧收敛，预览 4spp 与最终 48spp 质量严格区分 |

## 目录结构

```
progressive_render/
├── engine/
│   ├── pathtracer.py    # 真实蒙特卡洛路径追踪器 + PNG 编码 + 配置哈希
│   ├── scenes.py        # 三个复杂度递增场景 + 不可变配置构建
│   ├── kernel.py        # 预览争用内核：有界池/槽位/老化调度/取代/限额/失败
│   ├── simulation.py    # 多位评审者演练（6 个场景）
│   └── server.py        # 标准库 HTTP 前端
├── web/
│   ├── index.html       # 出图任务面板 + 结果区 + 当前配置视图
│   └── app.js           # 内核驱动控件、版本 stale-gate、渐进轮询
├── run_server.py        # 启动 Web 服务
├── run_simulation.py    # 命令行演练并落盘 JSON/TXT 报告
└── reports/             # 演练报告输出目录
```

## 怎样启动界面

```bash
cd progressive_render
python3 run_server.py                 # 默认 127.0.0.1:8000
# 可选：python3 run_server.py --host 0.0.0.0 --port 8000 --workers 6
```

然后浏览器打开 **http://127.0.0.1:8000/**。

- 左上：选评审者、场景、相机（滑块拖动即连续发预览）、材质反照率、预览优先级；
  「提交预览」与「稳定配置 → 最终出图队列」两个按钮；可勾选关闭自动取代以演练限额。
- 中间：预览帧与最终渲染两个独立结果区，显示 cfg_hash、有效 spp、首帧/完成延迟、
  进度条；过期帧有虚线警告且不会覆盖当前视图。
- 右侧：内核实时 KPI、出图任务面板（状态/spp/分片进度/年龄/停止按钮）、演练结果。

面板所有数据都来自 `/api/kernel` 快照，由预览争用内核驱动控件，而非静态假数据。

## 运行多评审者演练（记录延迟、有效采样数、完成情况）

```bash
python3 run_simulation.py                 # 全部 6 个场景
python3 run_simulation.py --scenario quota
```

场景：`camera_drag`（快速拖动版本取代）、`preview_flood`（预览洪泛下最终图不饿死）、
`quota`（评审者限额拒绝）、`failure`（失败反馈）、`long_final`（长任务中交互预览仍有限响应）、
`priority`（切换优先级改变调度顺序）。报告写入 `reports/rehearsal_<时间戳>.{json,txt}`。
也可在网页面板点「运行多评审者演练」，走的是同一个真实内核。

## 关键边界与诚实的质量口径

- 预览固定目标 **4 spp**、最终 **48 spp**，UI 明确标注，低清预览不会冒充最终质量。
- 进程池大小 = `min(8, CPU-2)`，累加器槽位 20 个，均在 worker fork 前预分配，
  这是硬资源上限；本环境 `/dev/shm` 仅 64MB，故刻意不依赖共享内存文件系统。
- 渲染是真实路径追踪（漫反射余弦采样、金属反射、玻璃折射/Schlick、面积光、ACES 近似）。
