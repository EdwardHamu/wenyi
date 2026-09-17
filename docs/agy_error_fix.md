# Agy CLI 输入协议错误修复方案

## 现象

`agy_errors.log` 中 `Synopsizer` 请求连续失败，Agy CLI 返回：

```text
failed to decode stream input: json: cannot unmarshal string into Go struct field streamInputMessage.message of type printmode.streamInputUserMessage
```

失败发生在 CLI 解析 stdin 阶段。CLI 已输出 `init` 事件，但没有开始模型调用，因此这不是网络、模型、登录态、配额或超时问题。

## 根因

Wenyi 当前在 `trans_novel/llm/providers/agy.py` 的
`build_agy_input_payload()` 中构造了以下 payload：

```json
{"event": "user", "message": "实际提示词"}
```

Agy CLI `1.1.28` 的 `stream-json` 协议要求 `message` 是用户消息对象，而不是字符串：

```json
{"event":"user","message":{"role":"user","content":"实际提示词"}}
```

使用本机 Agy CLI 对象格式进行验证后，CLI 能返回 `status: SUCCESS`；因此该格式是当前版本可用的协议。

## 修复内容

### 1. 修改 payload 构造

修改 `trans_novel/llm/providers/agy.py`：

```python
def build_agy_input_payload(prompt: str) -> str:
    """通过 stdin 发送 Agy stream-json 用户消息。"""
    return (
        json.dumps(
            {
                "event": "user",
                "message": {"role": "user", "content": prompt},
            },
            ensure_ascii=False,
        )
        + "\n"
    )
```

保留末尾换行，因为 Agy CLI 按 NDJSON 逐行读取 stdin。

### 2. 更新单元测试

修改 `tests/test_llm.py` 中 `test_agy_ndjson_input_and_cli_arguments` 的断言：

```python
data = json.loads(payload)
self.assertEqual(
    data,
    {
        "event": "user",
        "message": {"role": "user", "content": "你好世界"},
    },
)
```

JSON 模式的断言也应从 `data["message"]` 调整为：

```python
self.assertIn("valid JSON", json.loads(inv_payload)["message"]["content"])
```

### 3. 增加协议回归覆盖

测试至少应固定以下不变量：

- 顶层 `event` 为 `"user"`；
- `message` 为对象；
- `message.role` 为 `"user"`；
- `message.content` 为完整提示词；
- payload 以单个换行结束；
- 非 ASCII 提示词不会被转义破坏。

如果 CI 环境允许安装 Agy CLI，可增加一个可选的 CLI smoke test；普通单元测试仍应使用 mock，避免依赖登录态和网络。

## 验证命令

```powershell
uv run pytest -q tests/test_llm.py
uv run ruff check .
uv run ruff format --check .
git diff --check
```

还应使用本机已登录的 Agy CLI 做一次最小手工验证：

```powershell
agy --print= --input-format stream-json --output-format stream-json --disable-slash-commands --model gemini-3.8-flash --effort low
```

stdin 输入一行：

```json
{"event":"user","message":{"role":"user","content":"hello"}}
```

预期结果包含：

```json
{"event":"result","result":{"status":"SUCCESS", ...}}
```

## 影响与注意事项

该错误会影响所有使用 `agy` provider 的请求，不只 `Synopsizer`。修复前，重试只能重复发送相同的错误 payload，不能解决问题。

当前配置中的 `gemini-3.8-flash` 和 `C:\Users\11038\AppData\Local\agy\bin\agy.EXE` 已被 CLI 接受，不需要因为本次错误更换模型或 CLI 路径。

修复后应保留 `parse_agy_events()` 的现有逻辑；本次故障发生在输入解析之前，与输出事件解析无关。
