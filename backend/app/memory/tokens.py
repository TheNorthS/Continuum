# 提供异步锁、超时与线程卸载，避免本地计量阻塞事件循环。
import asyncio

# 序列化请求内容，用 UTF-8 字节数进行保守预算。
import json

# 计量不可用或窗口配置无效时返回统一业务错误。
from app.common.errors import AppError

# 复用 HTTP JSON 请求适配器及其错误处理。
from app.infrastructure.http import post_json


# 统一计算文本聊天请求的输入成本，以及压缩触发、目标和硬上限。
class TokenMeter:
    """Meter the complete text chat request; fallback is explicitly conservative, not exact."""

    # 接收模型配置与共享 HTTP 客户端，不额外创建连接池。
    def __init__(self, settings, client):
        # 保存计量模式、窗口配置及外部计数服务连接。
        self.settings, self.client = settings, client
        # 本地 tokenizer 延迟加载，未启用该模式时不占用模型资源。
        self.tokenizer = None
        # 串行化本地 tokenizer 初始化及调用，避免并发加载竞争。
        self.lock = asyncio.Lock()

    # 根据实际模型窗口和本次最大输出长度推导输入预算。
    def limits(self, model, output=1600):
        # 优先读取指定模型的窗口覆盖值，否则使用全局窗口。
        window = self.settings.context_model_windows.get(model, self.settings.context_window)
        # 安全余量至少为配置值或窗口的 3%，取较大者。
        headroom = max(self.settings.context_headroom, int(0.03 * window))
        # 硬输入上限等于窗口减去输出预留和安全余量。
        hard = window - output - headroom
        # 压缩阈值不能超过硬上限，防止触发压缩时已无输出空间。
        trigger = min(int(self.settings.context_trigger_ratio * window), hard)
        # 目标取配置比例与触发阈值的 85% 中较小者，避免反复贴近阈值。
        target = min(int(self.settings.context_target_ratio * window), int(0.85 * trigger))
        # 目标非正说明窗口无法满足当前输出预留规则。
        if target <= 0:
            # 明确报告配置错误，不继续构造必然超限的请求。
            raise AppError("context_config", "模型窗口不足以容纳输出预留与安全余量", 503)
        # 同时返回窗口、硬上限、触发值和目标值供调用方使用。
        return {"window": window, "hard": hard, "trigger": trigger, "target": target}

    # 保守估计不依赖实例状态，因此声明为静态方法。
    @staticmethod
    # 为 JSON 可序列化内容估计字节预算，不声称是供应商精确 Token 数。
    def conservative(value):
        # UTF-8 字节预算保守覆盖文本/JSON；不是供应商的真实 Token 数，不用于图片或音频。
        # 紧凑 JSON 编码后取 UTF-8 字节长度，再增加 64 单位的固定余量。
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) + 64

    # 用本地 tokenizer 的聊天模板计算请求成本，供线程池调用。
    def _local(self, body):
        # 仅首次调用需要加载 tokenizer。
        if self.tokenizer is None:
            # 按需导入可选依赖，默认保守模式无需安装 transformers。
            from transformers import AutoTokenizer

            # 从配置的本地路径构造 tokenizer，并缓存到实例。
            self.tokenizer = AutoTokenizer.from_pretrained(
                # 禁止联网下载及远程代码执行，只使用已经存在的模型文件。
                self.settings.context_tokenizer_path,
                # 只加载已存在的本地文件，不自动下载模型。
                local_files_only=True,
                # 不执行模型目录提供的远程自定义代码。
                trust_remote_code=False,
            )
            # 聊天模型必须提供匹配的模板，不能用裸文本分词冒充完整请求计量。
            if not self.tokenizer.chat_template:
                # 缺少模板时中止精确计量路径。
                raise ValueError("tokenizer must provide its matching chat template")
        # 若请求带工具定义，将其交给聊天模板一起计量。
        options = {"tools": body["tools"]} if body.get("tools") else {}
        # 按模型模板组装消息并得到 Token ID 序列。
        value = self.tokenizer.apply_chat_template(
            # 包含生成起始提示及工具定义，尽量覆盖实际请求结构。
            body["messages"],
            # 直接得到 Token ID 序列，便于按实际分词结果计数。
            tokenize=True,
            # 把助手生成起始标记也计入输入成本。
            add_generation_prompt=True,
            # 若存在工具定义，将其传给模型聊天模板。
            **options,
        )
        # JSON response format may add provider-side instructions, retained as a separate safety estimate.
        # Token 序列长度加上 JSON 输出格式的保守开销；后者并非精确供应商计量。
        return len(value) + (self.conservative(body["response_format"]) if body.get("response_format") else 0)

    # 按配置选择保守、本地 tokenizer 或外部计数接口。
    async def count(self, body):
        # 读取本次服务使用的计量模式。
        mode = self.settings.context_token_meter
        # 默认保守模式无需模型或外部网络。
        if mode == "conservative":
            # 把会占用输入上下文的请求字段统一交给保守估计器。
            return self.conservative(
                # 只包含实际存在的消息、工具和输出格式字段。
                {k: body[k] for k in ("messages", "tools", "response_format") if k in body}
            )
        # 对需要依赖或网络的路径统一捕获可预期故障。
        try:
            # 本地 Hugging Face 模式使用对应模型 tokenizer。
            if mode == "huggingface":
                # 防止多个请求同时初始化或使用共享 tokenizer。
                async with self.lock:
                    # 把同步加载与分词移到线程，保持事件循环可响应。
                    return await asyncio.to_thread(self._local, body)
            # 外部计数请求受整体超时约束。
            async with asyncio.timeout(self.settings.context_count_timeout):
                # 通过共享 HTTP 客户端请求供应商兼容的计数服务。
                value = await post_json(
                    # 复用注入的连接池。
                    self.client,
                    # 使用单独配置的 Token 计数端点。
                    self.settings.context_count_url,
                    # 构造服务鉴权头，不将密钥放到消息正文。
                    headers={
                        # 仅在发请求时读取 SecretStr 中的密钥值。
                        "Authorization": "Bearer " + self.settings.context_count_api_key.get_secret_value()
                    },
                    # 发送模型名及实际输入字段，不把无关业务状态传给计数服务。
                    json={k: body[k] for k in ("model", "messages", "tools", "response_format") if k in body},
                    # 同时设置底层 HTTP 请求超时。
                    timeout=self.settings.context_count_timeout,
                )
            # 按计数接口约定读取 input_tokens 字段。
            result = value["input_tokens"]
            # 要求非负整数，严格排除 bool 以及字符串或浮点返回值。
            if type(result) is not int or result < 0:
                # 协议不合法时拒绝该计数结果。
                raise ValueError("invalid token count")
            # 返回通过类型与范围检查的输入 Token 数。
            return result
        # 收敛协议、业务、超时、系统和可选依赖错误。
        except (ValueError, TypeError, KeyError, AppError, TimeoutError, OSError, ImportError) as exc:
            # 转换成统一的计量不可用错误，不静默切换到另一种预算口径。
            raise AppError(
                # 向调用方给出稳定错误码和可操作的配置提示。
                "token_meter_unavailable",
                # 向用户提示检查计数服务或本地 tokenizer 配置。
                "Token 计量失败，请检查计数服务或本地 tokenizer 配置",
                # 以 503 表示当前计量依赖不可用。
                503,
                # 保留原始异常链，供服务端诊断具体失败原因。
            ) from exc
