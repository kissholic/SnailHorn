# 项目说明
数字货币套利项目，策略与代码为实验性质，不建议在真实环境中使用。

## 环境要求
- **Python 3.11+**（配置解析依赖 `tomllib`，Python 3.10 及以下无此模块）
- 依赖通过 `python -m venv .venv && .venv/bin/pip install -r requirements.txt` 安装
- `requirements.txt` 使用精确版本锁定

## 项目结构
- `Source/` — 项目源代码，**平坦结构，无包层级**。目录下没有 `__init__.py`，所有模块平级放置。模块间使用 `import config` 直接导入（禁止 `from Source import config`、禁止使用相对导入）。
- `Saved/` — 运行时缓存与持久化数据目录，已加入 `.gitignore`。
- 从项目根目录运行 `python Source/main.py` 时，Python 会将 `Source/` 加入到 `sys.path`，因此所有导入均从 `Source/` 目录解析。

## 核心约定
- 所有需要从外部获取的数据，必须添加缓存并写入 `Saved/` 目录，优先复用缓存再拉取。
- 所有外部拉取操作必须有错误处理，避免程序崩溃。
- 代码注释和日志消息使用中文。
- **日志记录器必须使用 `logging.getLogger("snailhorn")`**，不要用 `logging.getLogger(__name__)` 或其他名称。
- **新增模块时不需要创建 `__init__.py`**，直接引用现有模块名即可。

## 启动命令
```bash
.venv/bin/python Source/main.py                        # 正常启动
.venv/bin/python Source/main.py --dry-run              # 试运行（不执行交易）
.venv/bin/python Source/main.py --log-level DEBUG      # 调试日志
.venv/bin/python Source/main.py -c /path/to/config.toml # 自定义配置
```

## 配置文件
- 默认从 `Saved/config.toml` 读取，可通过 `-c` 参数或 `SNAILHORN_CONFIG` 环境变量覆盖。
- 配置文件本身可选：缺失时自动在 `Saved/config.toml` 创建含默认值的配置文件。
- 日志默认写入 `Saved/snailhorn.log`。
- 交易所配置使用 TOML `[[exchanges]]` 数组。TOML 字段名（如 `api_key`、`api_secret`）通过 `config.py:_EXCHANGE_KEY_MAP` 映射为 ccxt 所需的 camelCase 键名（如 `apiKey`、`secret`）。新增交易所字段时**必须同步维护 `_EXCHANGE_KEY_MAP`**，该映射表是权威来源。
- **ccxt 代理陷阱**：ccxt 4.x 中 `proxy` 是旧式 URL 前缀代理（已废弃，实际不可用），标准 HTTP 代理应使用 `httpsProxy`。本项目在 `_EXCHANGE_KEY_MAP` 中已将 TOML 的 `proxy` 字段映射为 `httpsProxy`。

## 质量保障
- 本项目暂无测试套件、linter 或 typechecker 配置。对代码的改动无需运行验证命令。
