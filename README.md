# Email Dashboard

自行部署的邮件项目看板：优先行动、时间线、邮件记录、手动进展和 AI 助手。每人连接自己的邮箱，使用自己的模型 API，数据保存在自己的设备中。界面为中文。

本分支 `feature/personal-email-setup` 提供完整首次设置流程；`main` 保留脱敏后的原应用基线。仓库不包含私人邮件、项目历史、密钥、账户配置或数据库。

## Quick setup

需要 **Python 3.11+**，以及可运行 `app-server` 的 **Codex 客户端**。先在自己的 Codex / ChatGPT 中连接 Outlook 或 Gmail。此版本通过本机 Codex MCP 连接器读取邮箱，连接器必须对当前用户可用；无需把邮箱密码或 OAuth token 填入看板。邮箱接入不是独立的 Google / Microsoft OAuth 服务。

```sh
git clone --branch feature/personal-email-setup git@github.com:Philos-z/Email-Dashboard.git
cd Email-Dashboard
cp .env.example .env
./start.sh
```

也可以使用 HTTPS 克隆地址 `https://github.com/Philos-z/Email-Dashboard.git`。启动时会要求创建看板访问密码，然后打开 http://localhost:8765 。

1. 点击 **开始设置**，选择 Outlook / Gmail，填写自己的邮箱、时区和邮件起始日期。
2. 点击 **保存并验证邮箱**。验证只读取邮箱身份，不读取邮件、不调用模型。连接账号与填写的邮箱不一致时会停止。
3. 点击 **配置自己的 API / 模型**，填写 OpenAI 兼容的 API 基础地址、API key 和模型名称。支持公开 HTTPS API（例如 `https://api.openai.com/v1`）及本机 HTTP 代理。自定义基础路径会保留。
4. **保存并测试连接** 会调用模型一次，不附带项目数据。也可以直接保存，稍后再测试。
5. 在邮箱设置中选择是否允许向自己的模型发送新邮件的部分正文及项目摘要，然后打开 **刷新邮件**，立即刷新或主动开启每小时刷新。

自动刷新默认暂停。开启后每小时检查一次，所选时区的 00:00–06:00 暂停。每次最多处理 100 封，积压时再次刷新会继续；没有新邮件时不调用模型。关闭网页后，运行中的后端仍可刷新，关闭后端则停止。

Outlook 检查收件箱、存档和已发送及其子文件夹；Gmail 检查 Inbox、Archive、Sent，排除垃圾箱、垃圾邮件和草稿。推广会参与读取但不会默认创建项目。不会调用发信、删除、移动邮件或附件下载工具。

## 自定义与部署

配置项在 [.env.example](.env.example)，邮箱和 API 在网页中设置。`.env` 自动加载，已有进程环境变量优先。

| 项目 | 用途 |
| --- | --- |
| `ATLAS_DATA_DIR` | 独立的私有数据目录，默认 `./data`；不同使用者必须使用不同目录和运行实例 |
| `ATLAS_BIND` / `ATLAS_PORT` | 默认仅监听 `127.0.0.1:8765` |
| `ATLAS_REQUIRE_LOGIN` | 默认 `1`，启用看板密码；`0` 仅用于自己的本机测试 |
| `ATLAS_TIMEZONE` | 首次设置的默认时区；实际显示和暂停时段使用网页保存的时区 |
| `ATLAS_CODEX_BIN` | 可选的 Codex 可执行文件路径；否则从 PATH 或 macOS 应用中寻找 |
| `ATLAS_MCP_SERVER` | 默认 `codex_apps`；可在首次设置中改为提供相同工具契约的 MCP server |
| `ATLAS_HTTPS` | HTTPS 反向代理部署时设为 `1`，启用 Secure cookie |

默认部署是一人一个实例，同一实例内设备共享一份邮箱、项目数据和 API 配置。它不提供多人登录或租户隔离。需要局域网访问时，明确设置 `ATLAS_BIND=0.0.0.0` 并保留密码登录。保持单个后端进程；不要使用多个 workers。

邮箱、时区、起始日期和 MCP server 在首次成功同步后绑定到该数据目录。更换这些设置时，用新的 `ATLAS_DATA_DIR` 启动一个空白实例，避免混入其他账户的邮件。模型 API 可以单独更换；更换地址需重新输入密钥，不会把旧密钥自动发送至新服务。

私有数据目录保存 SQLite、邮箱设置、API key、访问密码摘要和会话密钥。密钥配置文件使用本机私有权限；不要提交或公开共享该目录。项目 JSON / CSV 导出不包含 API key；导出的数据库和项目记录仍可能包含私人邮件摘要。仓库的 `.gitignore` 排除了默认数据目录、密钥、日志、备份及截图。

Windows 或已有 Python 环境可以手动启动：

```sh
python -m venv .venv
# 激活该环境后执行：
python -m pip install -r requirements.txt
python build_frontend.py
python setup_password.py
python server.py
```

## 接口和适配

见 [接口与 MCP 契约](docs/INTEGRATIONS.md)。通用入口是 `/api/mail/*` 和 `/api/ai/*`；原 `/api/outlook/status|refresh|schedule` 保留为兼容别名，按当前选择的邮箱运行。

MCP 客户端只启动临时工具上下文，不启动模型会话；模型调用统一通过使用者配置的 API。工具需要新授权或交互确认时会停止，请在自己的客户端完成授权后重试。

## 开发与验证

```sh
python -m pip install -r requirements.txt -r requirements-dev.txt
python build_frontend.py
python -m pytest -q
node --test tests/*.cjs
```

前端由 `build_frontend.py` 生成；编辑 `web/lan_bridge.js`、`atlas_focus.js`、`atlas_mail.js`、主题文件或模板后需要重新构建。测试使用虚构项目、邮件和模型响应，不需要真实邮箱或付费 API。

验证覆盖账号匹配、设置保护、分页与重启续传、时间排序、已发送证据、模型错误回滚、并发编辑和密钥隔离。Gmail 适配已按当前连接器的工具参数及 MIME 契约完成模拟测试，尚未读取真实 Gmail 邮件做端到端验证；使用者可通过自己的验证按钮和首次手动刷新确认连接器兼容性。

代码采用 MIT 许可。字体 Noto Sans SC / Playfair Display 使用各自附带的 SIL Open Font License。
