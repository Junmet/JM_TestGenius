# JM_TestGenius

基于大语言模型（LangChain + OpenAI 兼容接口）的**需求文档 → 测试点 / 测试用例**生成工具。将 `input` 目录中的需求文档解析为文本，调用模型生成大纲（摘要、测试点、思维导图），再按测试点分批生成结构化测试用例，并输出到 `output` 目录。

## 功能概览

- **输入**：支持 **Word（`.docx`）**、**Markdown（`.md` / `.markdown`）**、**纯文本（`.txt`）**、**PDF（`.pdf`）**；从指定输入目录扫描同级文件（非子目录递归，见「使用说明」）。另支持 **HTTP(S) URL** 拉取网页正文，以及通过 **Confluence REST** / **飞书 docx API** 拉取 Wiki/文档（与本地文件可混用，见「远程需求」）。
- **生成流程**：
  1. 解析文档正文（过长时按 `--max-chars` 截断，避免超出模型上下文）。
  2. 调用 LLM 生成**大纲**：文档摘要、测试点列表、Mermaid 思维导图描述等。
  3. 按测试点**分批**生成测试用例（由 `--max-cases`、`--batch-size` 控制规模与单次请求量）。
- **输出**（每个源文件一套，文件名以**源文件主名**为前缀）：
  - `*.xmind`：思维导图（含测试点与测试用例分支）。
  - `*.testcases.md`：Markdown 表格用例。
  - `*.testcases.xlsx`：Excel 用例表（canonical 列）。
  - `*.meta.md`：元信息（假设、风险、范围等）。
  - **可选额外导出**（与 `*.testcases.xlsx` 同源列映射，便于导入 TMS；**默认不生成**，需 CLI `--exports` 或 Web 侧勾选）：
    - `*.testcases.csv`：UTF-8 BOM，与 Excel 列一致。
    - `*.zentao.csv`：禅道常见用例 CSV 列名（版本差异大，导入前请对照系统模板微调）。
    - `*.testlink.xml`：TestLink 1.9 风格 `testsuite`/`testcase` XML。
    - `*.jira.csv`：Jira 通用 CSV（Summary/Description/Priority 等，便于再加工或 Xray/Zephyr 适配）。
- **模型**：默认 **DeepSeek**（OpenAI 兼容）；可选 **通义千问**（阿里云 DashScope 兼容模式）。通过环境变量切换与配置密钥。
- **日志**：运行日志写入项目根目录 `log/`，按时间戳命名（该目录已在 `.gitignore` 中忽略）。可选 **`LLM_LOG_IO`**（见配置表）在日志中记录 LLM 请求的摘要；命令行 **`-v` / `--verbose`** 可将 `src` 包的 DEBUG/INFO 同步到终端，便于排障。

## 环境要求

- **Python 3.10+**
- 可访问所选模型厂商的 API（需自行申请 Key）

## 安装

```bash
# 克隆仓库后进入项目根目录
cd JM_TestGenius

# 建议使用虚拟环境
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

复制环境变量模板并填写密钥：

```bash
copy .env.example .env
# 或使用 cp .env.example .env
```

编辑 `.env`：至少配置 **DeepSeek** 的 `DEEPSEEK_API_KEY`，或切换到千问后配置 `DASHSCOPE_API_KEY`。说明见下表与 `.env.example` 注释。

**切勿将 `.env` 提交到 Git**（仓库已忽略）。

## 配置说明（`.env`）

| 变量 | 说明 |
|------|------|
| `LLM_PROVIDER` | `deepseek`（默认）或 `qwen`（通义千问） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | 可选，默认 `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 可选，如 `deepseek-chat`、`deepseek-reasoner` |
| `DASHSCOPE_API_KEY` / `QWEN_API_KEY` | 使用千问时填写 |
| `DASHSCOPE_BASE_URL` / `QWEN_MODEL` | 千问可选覆盖 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 通用覆盖，便于切换环境 |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | 请求超时与最大 token；未设置时回退见下行 | — |
| `DEEPSEEK_TIMEOUT` | 超时秒数回退（未设置 `LLM_TIMEOUT` 时） | `120` |
| `DEEPSEEK_MAX_TOKENS` | 最大 token 回退（未设置 `LLM_MAX_TOKENS` 时） | `16384` |
| `APP_LANGUAGE` | 输出语言：`zh` 或 `en`（也可用命令行 `--language` 覆盖） |
| `LLM_LOG_IO` | 可选。`1` / `true` / `yes` / `on` 时，在 `log/` 中记录每次 LLM 调用的**请求与响应摘要**（长度与各段截断预览；不含 Key，见 `.env.example`）。未设置或为 `0` 则不记录 |

## 远程需求（URL / Confluence / 飞书）

与 `input/` 本地文件**可同时使用**；每条来源独立跑一遍生成流水线。

| 方式 | 说明 |
|------|------|
| 普通网页 | `--url https://...` 可重复；或 `--url-file urls.txt`（每行一条，`#` 开头为注释）。使用 `httpx` 下载，正文用 `trafilatura` / BeautifulSoup 抽取。 |
| Confluence Cloud | 行首写 `confluence:` + 页面完整 URL（需含 `/pages/数字ID`）或只写数字页面 ID。需 `.env`：`CONFLUENCE_BASE_URL`、`CONFLUENCE_EMAIL`、`CONFLUENCE_API_TOKEN`。若 URL 为 `*.atlassian.net/wiki/...` 且已配置凭证，也可不写前缀而自动走 API。 |
| 飞书云文档 | 行首写 `feishu:` + 文档 URL（`/docx/文档ID`）或只写文档 ID。需 `.env`：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`（应用需开通 **云文档** 读取权限）。若 URL 含 `feishu.cn/docx/` 且已配置应用，可自动走 API。 |
| 飞书知识库 Wiki | 使用带 **`/wiki/节点token`** 的完整链接（例如 `https://xxx.feishu.cn/wiki/M26Kw...`）。程序会先调 [Wiki get_node](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node) 再拉 docx 正文。应用需开通 **知识库（wiki）** + **云文档（docx）** 相关读权限；节点须为 **新版云文档**（`obj_type` 为 docx）。 |

**Streamlit**：侧栏「远程需求 URL」文本框，每行一条，规则同上。

各平台权限与字段以官方文档为准；若 API 返回结构与预期不符，可在 `log/` 中查看报错并反馈。

## 启动与使用

**必须在项目根目录执行**（保证 `python -m src.main` 能解析包 `src`）：

```bash
python -m src.main
```

常用参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--input` | `input` | 需求文档所在目录 |
| `--output` | `output` | 生成结果输出目录 |
| `--language` | 读自 `APP_LANGUAGE` | 输出语言 `zh` / `en` |
| `--encoding` | `utf-8` | 文本 / Markdown 文件读取编码 |
| `--max-cases` | `80` | 每个文档最多生成的测试用例条数 |
| `--batch-size` | `10` | 每批调用模型生成的用例数（过大可能导致单次回复过长） |
| `--max-chars` | `15000` | 参与生成的正文最大字符数，超出截断 |
| `--sleep-after-call` | `0` | 每次 LLM 调用成功后休眠秒数，用于限流、降低打满配额概率 |
| `--sleep-between-files` | `0` | 每处理完一个文件再处理下一个前休眠秒数 |
| `--max-total-tokens` | `0`（不限制） | 单次任务累计 token 上限；优先累加接口返回的 token，否则按「请求+回复字符」÷4 估算，超限则停止后续调用 |
| `-v` / `--verbose` | 关闭 | 将本项目 `src` 包下的 DEBUG/INFO 日志输出到**终端**（详细排查仍以 `log/` 文件为准；不会影响 HTTP 库的刷屏级别） |
| `--llm-log-io` | 关闭 | 本次运行**强制开启** LLM 请求/响应摘要日志（写入 `log/`；与 `.env` 中 `LLM_LOG_IO` 二选一即可，CLI 显式传入以本次为准） |
| `--exports` | `none` | 额外导出模板（与 Excel 同源映射）：逗号分隔 `csv` / `zentao` / `testlink` / `jira`；**默认 `none`**（仅 xlsx/md/meta/xmind），需要时再写如 `--exports csv,zentao` |
| `--url` | — | 远程需求 URL，可多次指定（见「远程需求」） |
| `--url-file` | — | 从文件读取多行 URL（见「远程需求」） |

任务结束后，终端会输出 **LLM 用量**（调用次数、上报 token 累计、估算 token、请求/回复字符数），便于核对成本与配额。

## Web 界面（可选）

安装依赖后，在项目根目录执行：

```bash
streamlit run streamlit_app.py
```

浏览器中可选择 **输入/输出目录**、**额外导出格式**（默认不勾选；勾选后生成 CSV / 禅道 / TestLink / Jira 模板）、查看 **进度条**、任务结束后 **预览 Excel 用例表** 与 Markdown 节选。侧栏可选 **控制台详细日志**（等同命令行 `--verbose`，终端输出 `src` 的 DEBUG/INFO）与 **LLM 请求/响应摘要**（等同 `LLM_LOG_IO`）。需已配置 `.env` 中的 API Key。每次点击「开始生成」会写入项目根目录 **`log/`** 下带时间戳的日志文件（与命令行一致），并在运行 `streamlit run` 的**终端**同步输出 INFO 级别日志（勾选详细日志时为 DEBUG）。

示例：

```bash
# 使用默认 input / output，每个文档最多 40 条用例
python -m src.main --max-cases 40

# 指定目录与编码
python -m src.main --input ./docs --output ./out --encoding utf-8 --max-cases 50
```

查看全部参数：

```bash
python -m src.main --help
```


## 目录约定

- **`input/`**：放置待处理需求文档（`.gitignore` 已忽略，适合每人本地放文档，不提交仓库）。
- **`output/`**：生成结果（默认忽略，可按团队需要改为提交样例或保持忽略）。
- **`log/`**：运行日志（忽略）。

首次运行若不存在 `input` 或其中无支持格式的文件，程序会提示创建目录或放入文档后再执行。

## 依赖摘要

主要依赖：`langchain`、`langchain-openai`、`python-dotenv`、`python-docx`、`pymupdf`（PDF）、`pandas`、`openpyxl`、`rich`、`py-xmind16` 等，完整列表见 `requirements.txt`。

## 许可证

本项目采用 [MIT License](LICENSE)（见仓库根目录 `LICENSE` 全文）。
