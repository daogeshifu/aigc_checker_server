# 微软 Azure OpenAI 复核

实现参考 `wd-ranktool/app/Service/AIService.php` 的 `chatWithAzure`：

```text
POST https://你的微软资源/openai/v1/chat/completions
Authorization: Bearer <Azure API Key>
model: Azure 实际 deployment name
```

PHP 项目的直接调用不传固定 temperature 和 api-version。本项目默认按相同的 v1 方式调用，并增加 JSON 输出约束和结果校验，以满足检测/改写接口的返回结构要求。仍然请求微软资源，不会转到 OpenAI 公共 API。PHP 的 `$useOfficial=false` 是旧网关开关，本项目不使用该网关。

## 现有项目配置

在 `.env` 中保留原 Azure 密钥，设置：

```dotenv
AZURE_API_MODE=v1
AZURE_ENDPOINT=https://suzhou-gpt5.cognitiveservices.azure.com/openai/v1/
AZURE_TIMEOUT=600
AZURE_CONNECT_TIMEOUT=10
REMOTE_REVIEW_ENABLED=true

# 必须是 Azure 中实际存在的部署名。
AZURE_REVIEW_DEPLOYMENT=你的复核模型部署名称
# 留空沿用模型默认。模型支持时可设置 low 等推理强度。
AZURE_REVIEW_REASONING_EFFORT=
```

`AZURE_ENDPOINT` 填资源根地址也可，程序自动补一次 `/openai/v1/`，不会重复追加。不要填写完整 `/chat/completions` 请求地址。v1 不读取 `AZURE_API_VERSION`，无须将模型快照日期填入该字段。

随后执行：

```bash
sudo bash manage.sh update api
```

部署入口会保留显式设置的复核 deployment、API mode、超时和密钥，不会自动修改为某个新模型，也不会自动创建微软云端部署。

## 与 PHP 项目相同的环境变量

也可使用 PHP 项目中的命名，程序支持以下兼容项。两组都有值时，`AZURE_OPENAI_*` 优先于通用项；显式复核部署仍由 `AZURE_REVIEW_DEPLOYMENT` 决定。

| PHP 项目变量 | 对应本项目通用变量 |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | `AZURE_ENDPOINT` |
| `AZURE_OPENAI_API_KEY` | `AZURE_API_KEY` |
| `AZURE_OPENAI_DEPLOYMENT` | `AZURE_DEPLOYMENT` |
| `AZURE_OPENAI_TIMEOUT` | `AZURE_TIMEOUT` |
| `AZURE_OPENAI_CONNECT_TIMEOUT` | `AZURE_CONNECT_TIMEOUT` |

不会从另一个项目自动读取或复制密钥。配置应指向与密钥和 deployment 匹配的同一个微软资源。

## GPT-6 Sol 与参考项目实际配置

核对时 `wd-ranktool` 的代码和环境配置使用的是 `gpt-5.6-sol`；这不能证明同一微软资源已部署了 `gpt-6-sol`。用户指定的目标是 GPT-6 Sol，本次没有将它替换为 GPT-5.6 Sol 或 GPT-6.1 Sol，也没有擅自更换线上 deployment。

如果 Azure 中已创建 GPT-6 Sol，并将其部署名设为 `gpt-6-sol`，则可配置：

```dotenv
AZURE_REVIEW_DEPLOYMENT=gpt-6-sol
AZURE_REVIEW_REASONING_EFFORT=low
```

部署名称也可能是自定义名称，应填写实际名称；仅修改字符串无法部署模型。不要根据模型名猜测 Azure 资源权限或可用地域。

GPT-6 的非 none 推理模式不应传自定义 temperature。本项目默认 v1 模式全部省略该参数，兼容无法从名称识别模型类型的自定义 deployment；指定的推理强度通过 `reasoning_effort` 发送。模型能力以实际 Azure 部署为准。[官方模型说明](https://developers.openai.com/api/docs/models/gpt-6-sol)、[迁移参数说明](https://developers.openai.com/api/docs/guides/latest-model)。

复核仍遵循原有 Pro 权限和触发条件，仅调用 Azure；失败保留本地/百炼检测结果，不回退 OpenRouter。检测分数仍未校准为真实来源概率。

## 兼容旧日期版接口

旧资源必须走日期版接口时，使用：

```dotenv
AZURE_API_MODE=legacy
AZURE_ENDPOINT=https://你的微软资源/
AZURE_API_VERSION=资源实际支持的API版本
```

此模式保留旧的 Azure SDK 路由和 api-key 鉴权；没有配置推理强度时保留原 temperature=0.2。建议新模型使用 v1。Azure 客户端在业务进程退出时关闭连接池。

## 验证范围

测试通过真实 OpenAI Python SDK 和模拟 HTTP transport 验证 v1 路径、Bearer、JSON 字段、实际部署名、推理参数及 legacy 回退，没有调用付费模型。未验证微软账号权限、GPT-6 Sol 部署存在性或实际检测准确率。普通 `manage.sh check` 不发云端请求；上线后通过 Demo 的真实复核日志确认调用结果。
