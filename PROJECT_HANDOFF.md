# 众生行记（Library Terra）项目交接说明

> 更新时间：2026-08-29  
> 当前代码阶段：本地端到端原型 + ESP32-S3 实机联调 + OTA 首次验证阶段  
> 本文优先级高于 README 中可能尚未同步的阶段性描述。

## 1. 项目一句话说明

摄像头识别用户正在阅读的实体书及页码，Windows 本地服务把可信阅读状态、批注和反馈同步给手机网页及 Waveshare ESP32-S3 触摸屏。

## 2. 当前系统架构

```text
实体书 + 摄像头
      ↓
app.py（封面/页码识别、运动检测、多帧共识）
      ↓ HTTP :8765
living_margins_web.py（账号、设备、阅读会话、批注、设备网关、OTA）
      ↓ HTTP :8780
      ├─ web/ 手机网页
      └─ ESP32-S3 4.3 英寸触摸屏
```

数据分为两类：

- 可提交的内容数据：`books/<book-id>/book.json`、封面图片、`comments.json`。
- 本机运行数据：`runtime/`、`debug/`、设备密钥和编译目录；这些默认不进 Git。

## 3. 文件结构与职责

```text
library-terra/
├─ app.py                         # 电脑端识别主循环和桌面调试界面
├─ config.json                    # 摄像头、OCR、共识、状态服务配置
├─ enroll_book.py                 # 拍摄封面、OCR 建议书名、人工确认入库
├─ manage_books.py                # 书籍命令行管理工具
├─ comment_editor.py              # 本地批注管理窗口
├─ living_margins_web.py          # 手机网页/API 服务（当前 API v9）
├─ virtual_screen.py              # 早期 480×320 虚拟设备屏幕
├─ run.bat / run.ps1              # 启动识别端
├─ run_web.bat                    # 启动网页/API
├─ add_book.bat                   # 启动书籍录入
├─ add_comment.bat                # 启动本地批注编辑器
├─ library_terra/
│  ├─ vision.py                   # OCR 数字候选、页码排名、运动检测
│  ├─ books.py                    # 封面特征匹配和书籍共识
│  ├─ enrollment.py               # 封面检测、透视裁正、标题建议
│  ├─ comments.py                 # 文件型页码批注读取与选择
│  ├─ reading_state.py            # 状态快照、revision、:8765 HTTP 服务
│  ├─ web_database.py             # SQLite 用户/设备/会话/批注/反馈数据层
│  └─ telemetry.py                # 识别会话事件、关键帧和摘要
├─ web/
│  ├─ index.html                  # 单页应用骨架
│  ├─ app.js                      # 登录、绑定、阅读、批注、审核和设备状态
│  └─ styles.css                  # 移动端界面样式
├─ firmware/
│  ├─ platformio.ini              # Waveshare ESP32-S3 N16R8 构建配置，COM7
│  ├─ src/main.cpp                # LCD/触摸/Wi-Fi/配对/状态/反馈/OTA 主逻辑
│  ├─ src/lvgl_v8_port.cpp        # LVGL 与 Waveshare 显示触摸适配
│  ├─ src/lm_font_cjk_16.c        # 屏幕中文字体资源
│  ├─ include/firmware_version.h  # 当前待发布版本号（现为 0.9.1）
│  ├─ include/device_secrets.example.h # 设备密钥模板
│  ├─ include/esp_panel_board_custom_conf.h # 屏幕板级配置
│  └─ publish_release.py           # 生成 runtime/firmware OTA 发布物
├─ tests/                          # 识别、数据库、API 共 41 项自动测试
├─ books/                          # 已录入书籍；README 说明数据格式
├─ runtime/                        # SQLite、状态和 OTA 包（忽略提交）
├─ debug/                          # OCR 诊断与测试会话（忽略提交）
├─ README.md                       # 使用说明；部分阶段描述需要同步更新
├─ DEVELOPMENT_LOG.md              # 识别路线、现场证据、技术决策
└─ DESIGN_RULES.md                 # UI 视觉约束
```

## 4. 已完成并通过验证的功能

### 4.1 电脑视觉识别

- 摄像头整帧 OCR，不再依赖容易裁掉页码的固定 ROI。
- 运动迟滞、稳定帧筛选和异步 OCR，翻页时暂停识别。
- 普通页码需连续 2 次一致，大跳页需 3 次一致。
- 单次误识别或无结果不会覆盖已确认页码。
- 自动重新捕获、封面 ORB 特征匹配和双次封面确认。
- 拍照录入书籍：封面轮廓、透视裁正、OCR 标题建议、人工审查。
- 状态通过 `http://127.0.0.1:8765/state` 发布，语义变化才增加 revision。

### 4.2 手机网页与本地服务

- 注册、登录、30 天会话、退出。
- 设备绑定、二维码配对、阅读会话开始/暂停/恢复/结束。
- 灵感标记、批注草稿、提交审核、管理员批准/拒绝。
- 批准后的批注写入书籍 `comments.json`，识别端热加载。
- 赞同/不赞同可持久化、恢复和改变选择。
- `/api/devices` 展示设备在线、实时长轮询或普通轮询状态。
- 网页不会在 OCR 离线时误用旧 `runtime/state.json` 伪装实时状态。

### 4.3 ESP32-S3 实机

- 硬件：Waveshare ESP32-S3-Touch-LCD-4.3，800×480，16 MB Flash、8 MB PSRAM。
- LCD、GT911 触摸、中文字体和横屏布局已在实机验证。
- 板上 Wi-Fi 扫描/选网/密码输入和断线重配。
- 设备令牌认证；服务端仅保存哈希，原始密钥不进 Git。
- 二维码配对、实时状态、书籍/页码/批注显示。
- 赞同/不赞同触摸交互已修复并由用户复测通过。
- 长轮询实时模式已显示 `LIVE`，设备在线状态已测试通过。
- 短按右上角 `LIVE` 检查升级，长按仍显示配对二维码。

### 4.4 OTA 在线升级

- API v9 已加入认证的检查和下载接口。
- 服务器发布物包含版本、文件大小和 SHA-256。
- 板子下载前检查元数据，下载时写备用 OTA 分区并计算 SHA-256。
- 下载中断、大小不符、哈希不符或写入失败时不切换启动分区。
- OTA 引导版 `0.9.0` 已通过 COM7 烧录，入口检查测试通过。
- `0.9.1` 已编译并发布到 `runtime/firmware/`，但尚缺用户确认“无线安装后重启并显示 0.9.1”的最终实机验收记录。

### 4.5 自动验证基线

- Python：41 项测试通过。
- Web JavaScript：`node --check web/app.js` 通过。
- 固件：PlatformIO 编译通过，约 3.03 MB，占单个 6.4 MB OTA 分区约 46%。
- 0.9.0 有线烧录后设备哈希校验通过。

## 5. 当前运行和开发方式

### 5.1 首次安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 5.2 启动完整本地链路

1. 双击 `run.bat`，启动摄像头识别和 `:8765` 状态服务。
2. 双击 `run_web.bat`，启动 `:8780` 手机网页/API。
3. 电脑访问 `http://127.0.0.1:8780`；局域网设备访问 `http://<电脑IP>:8780`。
4. ESP32 与服务器必须都能访问同一服务器地址；无需彼此直连，但板子必须联网。

### 5.3 自动测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check web\app.js
```

### 5.4 固件编译和串口烧录

Windows 长路径可能导致 PlatformIO 失败，本机采用短核心目录：

```powershell
$env:PLATFORMIO_CORE_DIR='D:\p'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
C:\Users\15160\.platformio\penv\Scripts\platformio.exe run -d firmware
C:\Users\15160\.platformio\penv\Scripts\platformio.exe run -d firmware --target upload --upload-port COM7
```

只有首次安装 OTA 引导版或 OTA 故障恢复需要串口烧录。

### 5.5 发布 OTA

1. 修改 `firmware/include/firmware_version.h`，必须递增语义版本。
2. 编译固件并确认未超过 `0x640000` 字节。
3. 运行：

```powershell
.\.venv\Scripts\python.exe firmware\publish_release.py
```

4. 脚本原子写入 `runtime/firmware/firmware.bin` 和 `release.json`。
5. 板子短按 `LIVE`，核对版本，确认安装；升级期间不可断电。

## 6. 本机敏感和不可提交内容

- `firmware/include/device_secrets.h`：机器码、设备令牌、服务器地址等。
- `runtime/living_margins.db`：账号、设备、会话、反馈和批注草稿。
- `runtime/firmware/`：正在提供的 OTA 二进制及清单。
- `runtime/firmware-backups/`：原厂固件备份。
- `.pio/`、`.pio-core/`、`.venv/`、`debug/`。

提交前必须检查 `git status`，绝不能把 Wi-Fi 密码、设备令牌或数据库提交到仓库。用户曾在对话中提供过 Wi-Fi 密码，也不应复制到任何文档。

## 7. 剩余开发内容（按优先级）

### P0：完成 0.9.1 OTA 实机闭环

验收步骤：

1. 板子短按 `LIVE`，应发现 `0.9.1`。
2. 确认安装，观察下载、校验、自动重启。
3. 重启后再次短按 `LIVE`，应显示当前已是 `0.9.1`。
4. 确认书籍、页码、批注、触摸反馈和实时连接均未回退。
5. 把结果写入 `DEVELOPMENT_LOG.md`。

完成标准：不使用 COM7，完整升级一次且所有核心功能仍正常。

### P0：为 OTA API 补自动测试

目前 41 项回归测试未直接覆盖新增的二进制下载接口。需要新增：

- 正确令牌 + 旧版本返回可升级。
- 当前版本等于发布版本时返回无更新。
- 错误令牌返回 401。
- 下载内容、`Content-Length`、版本头、SHA-256 头一致。
- 清单文件缺失、越界路径、超大文件、哈希不符时拒绝发布。
- 版本降级不下发。

完成标准：测试无需真实板子，可在临时目录完整覆盖检查与下载协议。

### P0：整理并提交当前工作区

当前有大量已修改和新增但尚未形成清晰提交的文件。接手者应：

1. 逐项审查 `git diff`，确认无密钥和运行数据。
2. 更新 README 的过期文字：总体版本、二维码配对“下一步”等。
3. 将本交接文档、固件、API v9、设备状态和 OTA 分成可审查的提交。
4. 推送到正确的 GitHub 分支并记录提交哈希。

### P1：增强 OTA 的生产安全

- 增加固件签名校验，而不只依赖 SHA-256；哈希清单与二进制若同时被替换，当前方案无法抵御恶意包。
- 增加启动健康确认与自动回滚策略，并在真实断电/损坏包场景验证。
- 在服务端持久记录设备当前固件版本、升级开始/成功/失败原因。
- 增加下载进度、超时重试和更清晰的失败提示。
- 云环境必须使用 HTTPS，设备端验证服务器证书。

### P1：部署可远程访问的中继服务

当前服务器在电脑本地运行。产品化时两端不必在同一 Wi-Fi，但手机和板子都必须能访问一个公网 HTTPS 地址。建议：

- 将网页/API 部署到公网域名。
- 本地识别程序通过认证的 WebSocket 或 HTTPS 主动推送状态到云端。
- ESP32 通过 WebSocket/SSE/长轮询接收云端状态。
- 为设备、用户和家庭/空间建立明确租户隔离。
- 保留本地开发地址配置，避免每次改代码都部署云端。

### P1：真正的手机摄像头识别路径

目前手机网页不直接拍摄，识别仍由 Windows 摄像头完成。后续需要：

- HTTPS 下调用浏览器摄像头。
- 选择“手机上传帧到服务器识别”或“浏览器端轻量预处理”。
- 控制上传频率、分辨率、流量和隐私。
- 与电脑摄像头模式共用同一个阅读状态协议。

### P1：识别质量和现场验收

- 建立至少 20 次翻页、多个书籍和不同光照/距离的标准测试集。
- 使用 `Y/N/M` 人工标注真实正确率、误报率、漏检率和确认延迟。
- 继续处理页脚相对几何、底层纸页数字干扰、175/75 类 OCR 错误。
- 自动判断单页/左右页与装订方向，减少跨页歧义。
- 固定摄像头安装后重新标定画质和运动阈值。

### P2：账号、审核和数据生产化

- 删除“第一个账号自动成为管理员”的 Demo 规则，加入显式管理员初始化。
- 增加密码找回/改密、登录限速、CSRF/CORS 策略和审计日志。
- 设计 SQLite 迁移机制、备份与恢复；公网部署考虑 PostgreSQL。
- 明确批注隐私、公开范围、删除和内容审核规则。

### P2：交互与可维护性

- 网页和 ESP32 统一错误码与用户文案。
- 为屏幕增加设置入口：网络、服务器、设备信息、固件版本和诊断。
- 把 `firmware/src/main.cpp` 拆为网络、状态、UI、配对、反馈、OTA 模块。
- 生成 API 协议文档；明确 schema/version 兼容策略。
- 增加一键开发启动、健康检查和日志轮转。

## 8. 已知限制和风险

- README 顶部仍写 V6.7，且部分内容停留在二维码配对之前；不能据此判断最新能力。
- OTA 0.9.1 尚未取得最终实机成功反馈。
- OTA 目前是认证 + SHA-256 完整性校验，不等于数字签名。
- API/网页是本地 HTTP；公网使用前不能直接照搬。
- 服务器进程是开发模式，无进程守护、反向代理和正式日志管理。
- 设备服务器地址和令牌仍通过本机忽略文件配置，尚无生产级设备注册流程。
- 视觉识别有真实样本验证，但尚无足够规模的数据集证明泛化准确率。
- 设备与服务的协议演进没有正式兼容矩阵。

## 9. 建议接手顺序

1. 阅读本文件、`DESIGN_RULES.md` 和 `DEVELOPMENT_LOG.md`。
2. 运行 41 项测试，启动 `run.bat` 与 `run_web.bat`，确认 API v9 健康。
3. 完成 0.9.1 无线升级实机验收。
4. 补 OTA API 自动测试。
5. 清理 README 与 Git 提交，再开始云中继或手机摄像头开发。

## 10. 交接验收清单

- [ ] 能录入一本新书并重新识别封面。
- [ ] 能稳定识别页码，翻页时不会被单帧误识别覆盖。
- [ ] 手机网页能登录、绑定、开始阅读和提交批注。
- [ ] 管理员批准后，ESP32 能显示对应批注。
- [ ] 赞同/不赞同可切换并在刷新后恢复。
- [ ] ESP32 显示 `LIVE`，网页显示设备在线/实时。
- [ ] 不接串口完成一次 0.9.0 → 0.9.1 OTA。
- [ ] 自动测试全部通过。
- [ ] Git 中没有密钥、Wi-Fi 密码、数据库、运行日志和固件备份。

