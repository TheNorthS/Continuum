# 安全体系与使用说明

本版实现 JWT 会话、可管理 RBAC、资源授权、签名防重放、脱敏审计和应用层数据加密。**不宣称能抵御已经控制应用进程或浏览器 XSS 的攻击者**；这些边界仍需操作系统、网络、浏览器与部署运维配合。

问答已增加独立输入/来源/输出内容审查、分类阈值与故障停止机制。幻觉检查和有害内容过滤是不同门控，详见 [内容安全审查](MODERATION.md)。

## 1. 账号、角色和权限

管理员登录后进入 **账号与权限**：创建账号、修改角色、启用/停用、重置密码、撤销用户全部登录会话；自定义角色按权限点配置；审计日志可分页查询及校验完整性。
普通用户进入 **账号安全** 修改自己的密码，查看和撤销登录会话。管理员创建/重置的密码必须在首次登录修改；改密、停用或改变账号角色后，旧 JWT 和刷新令牌立即失效。最后一位有效管理员不能被停用或降级。

内置 `admin` 角色不可编辑。系统管理员可在“账号与权限 → 角色与权限”选择 `user（内置）`，勾选后保存，修改所有使用 `user` 角色账号的权限；新注册账号沿用修改后的权限，重启不会恢复默认值。两个内置角色均不能删除。只需修改单个账号时，创建自定义角色并分配给该账号。权限点包括问答、资料读取/写入/创建/分享、图谱读取/重建、监控、账号、角色、审计和会话管理。
授权操作在数据库中串行化，管理员不能通过并发操作绕过“最后一位管理员”约束。持有自定义管理权限的用户不能授予自己没有的权限，也不能修改更高权限账号或系统管理员。

CLI：`uv run --inexact python -m app.cli create-user alice --role user`。这是本机运维入口，必须由有权访问服务器和密钥的操作员执行。密码通过隐藏输入读取，创建账号默认要求首次改密。

## 2. 知识库资源授权

在知识库页面创建**默认私有**的知识库，选择它后再上传文档。创建者可向用户 ID 或角色授予 read/write/manage，并可撤销。
有效权限 = 角色权限点 + 对该知识库的授权。拥有 write 角色权限不等于能修改其他人的知识库。

- 文档列表、检索、图谱节点/边、路径、重建和删除都检查授权。
- 向量检索在查询前按允许文档过滤，结果返回时再次校验；本地索引快照与检索缓存使用可见知识库版本、权限版本和数据库命名空间生成的指纹隔离，详见[检索版本](RETRIEVAL_REVISION.md)。
- 图谱实体 ID 按知识库区分，避免跨私有知识库合并实体类型；关系只在授权文档集合中遍历。
- 已保存回答带文档 ID，授权撤销后历史回答中引用相关资料的内容会隐藏；对话仍只属于本人，管理员不能直接读取他人聊天。
- 旧版资料迁入“迁移的共享知识库”，保留原先 user 角色的只读权；管理员可在管理授权中撤销共享。

本版支持用户/角色级授权。团队可用自定义角色表达；未引入独立组织目录或企业 SSO。

## 3. JWT、会话与请求防重放

业务接口始终要求认证，没有开发环境跳过认证的开关。生产还强制独立 JWT 密钥、独立管理员密码、HTTPS 源地址、持久加密密钥与关闭自动 schema 迁移。

浏览器为每个标签页会话生成 P-256 ECDSA 密钥。私钥不可导出，使用 IndexedDB 保存 CryptoKey；JWT 绑定公钥指纹和服务端会话 ID。刷新令牌仅存哈希，轮换后旧令牌再次使用会撤销该会话。

每次请求通过 `X-Request-Proof` 签名，覆盖方法、URL path+query、**实际 HTTP body 字节**、令牌摘要、签发时间与随机 jti。默认只接受 60 秒窗口内的签名。数据库唯一约束消费一次性编号，多个进程也不能接受同一证明两次。
登录/注册另外使用 90 秒有效且仅可消费一次的服务端 challenge。多部分文件上传也对最终 multipart 字节签名。

这是应用专用 `rage-proof+jwt` 请求证明协议，借鉴 [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449.html) 的会话公钥绑定与 jti 防重放；增加正文摘要和 query 绑定，**不声称是完整 OAuth DPoP 实现**。
防护目标是拒绝被截获请求的重复提交、被盗 JWT 单独使用、正文/路径篡改；无法防止已经获得私钥调用能力的恶意脚本重新生成合法请求。HTTPS 和 XSS 防护仍必要。

旧版只带 Bearer 的脚本需要升级。Python 使用 `backend/app/security_client.py` 的 `SecurityClient`；浏览器 `services/api.ts` 自动处理 challenge、签名、刷新与重试。

## 4. 审计与脱敏

审计记录操作者 ID、动作、资源 ID、结果、时间、请求 ID和必要的结构化详情。不保存请求正文、文件内容、密码或令牌；来源 IP 使用 keyed HMAC 标识，便于关联而不落原始 IP。
统一日志过滤器覆盖令牌、API Key、密码键值、邮箱、手机号、身份证号、含凭据 URL 及 query。Langfuse 默认不采集正文；启用内容采集时先脱敏，生产直接禁止正文追踪。

账号/角色/授权变更与审计事件处于同一 SQL 事务；业务提交/删除/图谱状态变化有专门事件。每个 HTTP 操作另记状态。写操作的专门审计失败会回滚其事务；HTTP 访问审计在响应后写入，写入故障会输出 critical 日志，需要运维监控。
审计链每项包含前项签名，由独立审计密钥 HMAC 签名并维护数据库头记录，能检测修改、插入和不一致截断。`audit-verify` 或页面校验可检查。拥有数据库和审计密钥的攻击者仍可能重建链，生产应把审计导出到独立只追加存储并定期保存链头。

## 5. 数据加密、索引与密钥轮换

使用 [AES-256-GCM](https://cryptography.io/en/stable/hazmat/primitives/aead/)：每次写入随机 96-bit nonce，字段/缓存命名空间作为 AAD；密文含版本和 key ID，校验失败拒绝读取。

| 存储 | 处理 |
|---|---|
| SQL | 会话标题、消息内容/详情、文档名称/错误、chunk 正文/元数据/向量、知识库名称均加密 |
| 记忆与压缩 | 事件正文、原始资料、摘要、候选、私人记忆及其向量加密；归属/版本/状态用于事务查询，来源在注入和提交前再次复核 |
| Redis 与 L1 缓存 | 结果对象序列化后加密；旧版明文缓存前缀在启动时清理 |
| Neo4j | 实体名称/类型、关系类型/证据/文件名加密；结构 ID、关联和文档归属保留用于授权查询 |
| Milvus | 原文和元数据放在 AES-GCM 加密 payload；BM25 使用 HMAC 词项；向量保持可检索数值 |
| 密码与刷新令牌 | 密码使用独立随机盐 scrypt，不可逆；刷新令牌只保存 SHA-256 |

**向量、倒排词频、图拓扑、账号名和权限元数据不等于加密正文**，仍可能泄露统计或结构信息。ANN 必须对可计算向量检索；不能把随机加密向量谎称为可检索语义向量。生产数据库/对象存储/快照的完整静态加密须使用宿主机磁盘或云 KMS 加密卷，服务间远程传输须启用 TLS。
知恒（Continuum）不修改宿主机的磁盘加密配置。上传解析后不额外保存明文原文件；`data/documents/examples/` 和 `data/sample/` 的合成示例是普通文件，不会自动加密。自行保存的输入文件和本地历史资料需要独立管理访问权限。

开发环境首次启动生成 `.secrets/keyring.json`（Git/Docker 忽略），包含数据密钥环、稳定 blind-index 密钥和独立审计密钥；生产必须挂载已有密钥文件，拒绝自动生成。不要把密钥与数据库备份放在同一信任域。Linux 文件模式 0600；Windows 应由部署者把 `.secrets` 的 NTFS ACL 限定到运行账号，轮换使用 [ReplaceFileW](https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-replacefilew) 保留原文件 DACL。
Compose 使用独立 `encryption-keys` 卷，不写进镜像；K8s 使用单独 `rag-e-encryption` Secret 挂载。生产推荐通过 Secret Manager/CSI 挂载相同文件格式。

```bash
# 后端目录；先初始化开发密钥，再分发到安全的生产 Secret 存储
uv run --inexact python -m app.cli keys-init
uv run --inexact python -m app.cli keys-status
uv run --inexact python -m app.cli audit-verify
# 轮换：先停止全部 API worker 和 CLI 写入进程
uv run --inexact python -m app.cli keys-rotate --offline
```

轮换添加新的随机数据密钥，重加密 SQL、Neo4j、Milvus payload 并清理缓存；旧数据密钥保留以读取旧备份，不自动销毁。blind-index 和 audit 密钥保持稳定，避免检索/审计链断裂。轮换后重启全部 worker；不能一边写入一边执行离线轮换。
如执行中断，旧/新数据均有 key ID 且旧密钥保留，可再次运行继续迁移。密钥丢失无法解密，不能用重新生成文件替代原密钥。

## 6. 现有数据迁移与生产部署

已提供 `scripts/security_upgrade.py`：停止本机 API 后，创建并校验 AES-GCM 加密 SQLite 快照，再升级 schema、加密历史字段，并比较账号/会话/消息/文档/chunk 数量。SQLite 开启 secure_delete 并 checkpoint/VACUUM；存储设备历史快照、SSD 已释放块等仍需要全盘加密/备份管理。
PostgreSQL 先 `pg_dump` 到受保护/加密备份位置，单独执行 `alembic upgrade head`，再 `python -m app.cli init` 完成数据转换。多副本首次升级应先停写、单实例迁移，之后再启动全部副本。
Milvus 新建 `<collection>_secure`；只有旧索引中的每条记录都能对应 SQL chunk，且新索引写入/数量校验通过后，才删除配置指定的旧明文索引；发现额外数据会拒绝自动迁移，保留旧索引等待人工导出。

生产还需设置：

```dotenv
RAGE_ENVIRONMENT=production
RAGE_PUBLIC_ORIGIN=https://rag.example.com
RAGE_ENCRYPTION_KEY_FILE=/run/rag-e-secrets/keyring.json
RAGE_TRUSTED_PROXY_HOSTS=["你的受控反向代理IP或CIDR"]
```

后端只接受直接 HTTPS 或来自受信代理且 `X-Forwarded-Proto=https` 的请求；不要信任任意公网来源。Nginx/Ingress 必须覆盖外部传入的转发协议头。K8s 示例 `10.0.0.0/8` 只是占位网络，按真实代理范围收紧，并用 NetworkPolicy/防火墙阻止绕过入口。
健康探针允许内部 HTTP；生产隐藏 OpenAPI/Swagger。Prometheus 专用 metrics token 只允许读取 metrics，不具备业务权限，按部署网络控制抓取来源。
