# Pi 审计错误告警与强制退出实施计划

## 目标

当 `trans_novel/llm/providers/pi.py` 在运行中捕获到 Pi 调用错误，并且异常文本中包含
`审计` 两个字时：

1. 不再执行 Pi 的后续重试；
2. 向 `https://meamoe.top/koa/notify` 发送错误级别广播通知；
3. 同时通过 Gmail SMTP 向 `a50541853@gmail.com` 发送邮件；
4. 两个通知通道完成或达到 15 秒总等待上限后调用 `os._exit(1)`，立即终止整个
   Python 进程。

该行为只适用于 Pi provider，不改变其他 LLM provider 的错误处理和重试语义。

## 检测边界

- 在 `_log_pi_error()` 这一 Pi 错误统一落盘入口执行检测，但只允许
  `operation in {"parse_pi_events", "pi_cli_attempt"}` 触发。这覆盖 Pi JSONL 中声明的
  `errorMessage` 和 CLI 非零退出异常，同时明确排除 `pi_complete` 配置/装配错误、
  `complete_json_parse` 正常回复格式错误及 `pi_temp_file_cleanup` 清理错误。
- 只检查异常本身及其显式异常链的文本，也就是错误日志中 `Exception:` 和
  `Traceback:` 所表达的报错信息。
- 不独立检查正常 assistant 回复、请求输入、单独记录的 `STDOUT`、`STDERR` 或
  `MODEL RESPONSE` 载荷，避免正文偶然包含 `审计` 时误退出。若 CLI 非零退出时已有一段
  stderr 摘要被明确写入所抛出的 `RuntimeError`，该摘要属于异常消息，仍参与匹配。
- 匹配规则为区分大小写的字面包含判断：`"审计" in error_text`。
- 示例 `RuntimeError: pi agent 返回错误：OpenAI API error (403):
  {"message":"内容审计命中风险规则，请调整输入后重试",...}` 必须触发。
- 普通 403、网络错误、JSON 解析错误，以及成功响应正文中出现 `审计` 均不得触发。

## 实现设计

### Pi provider

在 `trans_novel/llm/providers/pi.py` 中增加以下内部能力：

- `_pi_error_contains_audit(error)`：用已访问对象 ID 集合防止异常链循环，遍历异常及
  `__cause__`/`__context__`，只拼接异常类型和异常消息进行关键字判断，不格式化 traceback
  源码行，也不读取异常对象上的 `stdout`、`stderr` 或 `response` 属性。
- `_send_audit_broadcast(context)`：使用项目已有的 `httpx` 依赖 POST JSON 到
  `https://meamoe.top/koa/notify`，单次超时 10 秒；同时校验 HTTP 状态码和响应 JSON 的
  `code == 200`。
- `_send_audit_email(context)`：使用 Python 标准库 `smtplib.SMTP_SSL` 和
  `email.message.EmailMessage` 发送 UTF-8 纯文本邮件，连接超时 10 秒。
- `_hard_exit_after_audit()`：唯一封装 `os._exit(1)` 的函数，生产环境不可返回，测试中
  替换为抛出自定义 `BaseException`，避免真正结束 pytest 进程，也避免被现有
  `except Exception` 吞掉。调用 `os._exit(1)` 前必须先调用现有
  `_cli.terminate_all_active_processes()`，清理其他并发请求仍在运行的 CLI 子进程树；清理
  失败只记录，不得阻止硬退出。
- 为 Pi system prompt 临时文件增加模块级线程安全注册表：创建成功后登记，正常 `finally`
  删除后注销；硬退出前尽力删除全部已登记文件，避免 `os._exit` 跳过 `finally` 后留下临时
  prompt。单个删除失败只记脱敏错误并继续退出。
- `_handle_audit_error(...)`：使用“已开始”状态、完成事件和专用锁选出唯一告警所有者；
  其他并发命中者只等待所有者完成，不重复发送。所有者用两个 daemon 线程并发尝试广播
  与邮件，一个通道失败不得阻止另一个通道；两线程共享 15 秒总等待期限，失败详情直接
  追加到现有错误日志并写入 stderr，最终无条件调用 `_hard_exit_after_audit()`。

`_log_pi_error()` 应先尝试把原有完整诊断写入 `pi_errors.log` 并关闭文件，再设置原有
`_pi_error_logged` 标记，最后对允许触发的 operation 执行审计检测和告警。原函数当前在日志
写入失败时会提前 `return False`，实现时必须重构为保存写入结果、继续检测，确保日志失败
不会跳过告警。通知错误追加日志时使用不带审计检测的底层 append helper，避免递归触发。
由于最终使用 `os._exit(1)`，不会进入 Tenacity 重试、上层线程池收尾或普通 `finally` 清理，
这正是“立即退出整个程序”的既定语义。

### 通知内容

广播请求体采用对象形式：

```json
{
  "title": "Wenyi Pi 内容审计告警",
  "content": "包含阶段、档位、请求 ID、尝试次数和异常摘要的文本",
  "level": "error",
  "sentAt": "ISO 8601 时间"
}
```

邮件主题使用 `Wenyi Pi 内容审计告警`，正文包含相同上下文以及完整异常消息。通知中不包含
翻译请求正文、系统提示、单独附加的完整 stdout/stderr 或正常模型回复，避免扩大可能敏感
内容的传播范围。广播和邮件发送失败日志只记录通道、异常类型及脱敏后的简短原因，绝不记录
SMTP 用户名、口令、完整邮件配置对象或 YAML 原始行。

### 本地邮件配置

在项目根目录创建 `pi_alert_mail.yaml`，并将 `/pi_alert_mail.yaml` 加入根 `.gitignore`。
运行时使用 `PI_ALERT_MAIL_CONFIG_FILE = "pi_alert_mail.yaml"`，相对路径按当前工作目录解析，
与现有 `PI_ERROR_LOG_FILE` 和默认 `config.yaml` 的行为一致。本项目的既定运行方式是在项目
根执行 `uv run trans-novel ...`，因此会读取根目录文件；测试通过 patch 该常量使用临时文件。
文件包含：

```yaml
smtp_host: smtp.gmail.com
smtp_port: 465
username: <Gmail 账号>
password: <Gmail 应用口令>
from_email: <发件地址>
to_email: a50541853@gmail.com
```

实际文件使用 `D:/MCode/pj/lexue_rs/src/util.rs` 当前配置的 Gmail 账号和应用口令，所有值
均写成带引号的 YAML 字符串；但任何受版本控制的源码、测试、示例、日志或文档都不得出现
真实口令。使用 `yaml.safe_load`；加载结果必须是 mapping，`smtp_port` 必须可转换为 1 至
65535 的整数，其余字段必须是非空字符串。配置缺失、YAML 无效或字段无效时，只记录固定的
脱敏原因，广播通道仍照常尝试，程序最终仍以状态码 1 退出。不修改 `LLMConfig`、默认
`config.yaml` 模板或用户现有配置。

## 测试计划

在 `tests/test_llm.py` 的 Pi provider 测试中使用 mock，禁止真实网络、SMTP 和进程退出：

- `parse_pi_events()` 收到示例中的审计错误时，断言广播和邮件各调用一次、不会进入重试，
  且最终请求强制退出码为 1；测试用自定义 `BaseException` 替换硬退出函数。
- 普通 Pi 错误不含 `审计` 时，保留原异常与现有重试/日志行为，不调用通知或强制退出。
- stdout、stderr 附加载荷或成功 assistant 文本单独包含 `审计` 时，不触发告警；CLI 非零
  退出异常消息自身包含 `审计` 时必须触发。
- 广播失败时仍尝试邮件；邮件失败时仍完成广播；两者失败时仍强制退出。
- 邮件配置缺失、YAML 无效或必填字段缺失时，不泄露凭据，不影响最终强制退出。
- 通知接口返回非 2xx 或 JSON `code != 200` 时视为广播失败。
- 用 fake monotonic clock/event 模拟一个通知线程超过总等待期限，不实际等待 15 秒，并验证
  硬退出在期限到达后发生。
- 两个线程并发命中时，重置模块级一次性状态并使用同步屏障控制交错顺序，验证只产生一组
  通知，两个调用均请求硬退出，避免依赖线程调度造成偶发失败。
- `pi_complete`、`complete_json_parse` 和 `pi_temp_file_cleanup` 的异常文本即使含 `审计`
  也不触发告警。
- 硬退出包装函数先调用 `terminate_all_active_processes()`，即使子进程清理抛错也仍调用
  `os._exit(1)`。
- 硬退出前删除模块注册的 Pi prompt 临时文件；删除失败不阻止退出，正常成功/普通失败路径
  仍保持现有 `finally` 清理行为。

## 文档与验证

- 在 `docs/configuration.md` 和 `docs/zh/configuration.md` 的 Pi provider 小节同步说明审计
  告警、`pi_alert_mail.yaml`、通知端点和强制退出语义。
- 不修改当前用户已有改动的根目录 `config.yaml`，也不把本地凭据文件加入 Git。
- 依次运行：

```powershell
rtk uv run --no-sync pytest -q tests/test_llm.py
rtk uv run --no-sync ruff check trans_novel/llm/providers/pi.py tests/test_llm.py
rtk uv run --no-sync ruff format --check trans_novel/llm/providers/pi.py tests/test_llm.py
rtk git diff --check
```

验收时还应通过 `git check-ignore -v pi_alert_mail.yaml` 和 `git status --short --ignored`
确认凭据文件确实被根 `.gitignore` 排除，并确认现有 `config.yaml` 修改未被覆盖或纳入本任务。
