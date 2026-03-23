# JM_TestGenius

基于大语言模型（LangChain + OpenAI 兼容接口）的**需求文档 → 测试点 / 测试用例**生成工具。将 `input` 目录中的需求文档解析为文本，调用模型生成大纲（摘要、测试点、思维导图），再按测试点分批生成结构化测试用例，并输出到 `output` 目录。

## 功能概览

- **输入**：支持 **Word（`.docx`）**、**Markdown（`.md` / `.markdown`）**、**纯文本（`.txt`）**、**PDF（`.pdf`）**；从指定输入目录递归扫描同级文件（非子目录递归，见「使用说明」）。
- **生成流程**：
  1. 解析文档正文（过长时按 `--max-chars` 截断，避免超出模型上下文）。
  2. 调用 LLM 生成**大纲**：文档摘要、测试点列表、Mermaid 思维导图描述等。
  3. 按测试点**分批**生成测试用例（由 `--max-cases`、`--batch-size` 控制规模与单次请求量）。
- **输出**（每个源文件一套，文件名以**源文件主名**为前缀）：
  - `*.xmind`：思维导图（含测试点与测试用例分支）。
  - `*.testcases.md`：Markdown 表格用例。
  - `*.testcases.xlsx`：Excel 用例表。
  - `*.meta.md`：元信息（假设、风险、范围等）。
- **模型**：默认 **DeepSeek**（OpenAI 兼容）；可选 **通义千问**（阿里云 DashScope 兼容模式）。通过环境变量切换与配置密钥。
- **日志**：运行日志写入项目根目录 `log/`，按时间戳命名（该目录已在 `.gitignore` 中忽略）。

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
