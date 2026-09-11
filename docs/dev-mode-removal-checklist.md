# 开发模式快速登录上线前移除清单

本文对应 `.specify/tasks.md` 的 Phase 8。开发模式只服务本地演示和测试；生产部署必须完成以下检查，并保留后端认证与授权边界。

## 需要删除的代码

| 文件路径 | 代码位置 | 删除原因 | 删除方式 |
| :--- | :--- | :--- | :--- |
| `backend/app/services/auth_service.py` | `ensure_dev_mode_accounts()`（T120）及其预设账号常量 | 预设账号和自动初始化会提供未经过正常密码输入的入口，不应存在于生产代码路径 | 上线构建中直接删除函数、常量及导出；同时删除调用点 |
| `backend/app/core/config.py` | `AppSettings.DEV_MODE` 字段及公开配置摘要中的对应项（T119） | 功能彻底移除后不应保留可重新开启快速登录的配置入口 | 直接删除字段、摘要键及相关测试/文档引用 |
| `backend/app/ui/gradio_app.py` | 登录面板中的“开发模式快速登录”区域及三个角色按钮（T121） | 生产登录页不应暴露角色选择或跳过密码验证的入口 | 直接删除组件、事件绑定和快速登录回调 |
| `backend/app/ui/layout_view.py` | 开发模式顶部提示横幅（T122） | 该提示只用于本地环境，生产界面不应保留开发模式文案 | 直接删除横幅组件及其条件渲染代码 |
| `.env.example` | `DEV_MODE=false` 示例项及开发模式说明（T123） | 若产品完全移除该能力，示例配置不应继续暗示存在快速登录开关 | 直接删除配置项和相关注释 |
| `docs/dev-mode.md` | 全文（T124） | 功能移除后避免维护过期的开发入口说明 | 直接删除，或在历史文档目录归档并标注已失效 |

## 可以通过配置关闭的代码

| 文件路径 | 配置项 | 关闭方式 | 关闭后行为 |
| :--- | :--- | :--- | :--- |
| `backend/app/core/config.py` | `DEV_MODE` | 在 `.env` 中设为 `false`（默认值也是 `False`） | 不创建预设测试账号；正常登录、密码验证、JWT 签发和现有 UI 保持不变 |
| `backend/app/services/auth_service.py` | `DEV_MODE` 的 `ensure_dev_mode_accounts()` 调用 | 确认运行环境读取到 `DEV_MODE=false` | 初始化辅助函数不执行；既有认证服务仅接受正常账号密码 |
| `backend/app/ui/gradio_app.py` | `DEV_MODE` 的快速登录区域 | 在 `.env` 中设为 `false` 并重启应用 | 登录页完全不显示快速登录区域，用户名/邮箱和密码登录路径照常工作 |
| `backend/app/ui/layout_view.py` | `DEV_MODE` 的视觉提示 | 在 `.env` 中设为 `false` 并重启应用 | 不显示开发模式横幅，角色导航和页面权限不变 |

## 必须保留的代码（不能删）

| 文件路径 | 代码位置 | 保留原因 |
| :--- | :--- | :--- |
| `backend/app/services/auth_service.py` | `create_access_token()`、`decode_access_token()`、`AuthService.issue_access_token()`、`AuthService.get_current_user()` | 负责 JWT 签名、有效期、主体和数据库用户状态校验，是身份鉴权边界 |
| `backend/app/services/auth_service.py` | `AuthService.authenticate()`、`verify_password()` | 正常登录仍必须校验用户名/邮箱、账户启用状态和密码哈希 |
| `backend/app/api/auth.py` | `POST /api/auth/login`、`GET /api/auth/me` | API 端点必须继续使用标准认证服务和 Bearer JWT，不得增加开发模式旁路 |
| `backend/app/core/security.py` | `get_current_user()`、`AuthenticationMiddleware`、`get_user_roles()` | 每个受保护请求必须验证 Bearer JWT、加载当前用户并按最新数据库状态处理 |
| `backend/app/core/security.py` | `RoleGuard`、`PermissionGuard`、`require_role(s)`、`require_permission` | 后端 RBAC/权限守卫是真正的安全边界，不能由 UI 隐藏按钮替代 |
| `backend/app/ui/gradio_app.py` | `_authenticated_state()`、`_guard_view_callback()`、`_can_navigate()` | Gradio 入口也必须重新验证 token、启用状态和导航角色，防止仅靠组件可见性越权 |
| `backend/app/ui/layout_view.py` | `navigation_items_for_roles()`、`is_authorized_navigation()` | 仅控制 UI 导航可见性和入口校验，不承担后端安全职责 |

## 上线前检查清单

- [ ] 确认所有生产环境 `.env` 中 `DEV_MODE=false`，并重启应用使配置生效。
- [ ] 确认预设测试账号已从数据库中删除；删除后抽查其旧 JWT 请求返回未认证错误。
- [ ] 确认登录界面不再显示开发模式快速登录区域或角色按钮。
- [ ] 确认工作台不再显示开发模式横幅。
- [ ] 使用真实账号验证用户名/邮箱 + 密码登录仍能签发 JWT。
- [ ] 使用无效、过期或已停用账号验证 API 仍返回认证失败。
- [ ] 使用 Teacher、Student、Admin 分别验证允许操作和越权操作，确认 RBAC 拦截率保持 100%。
- [ ] 确认 Admin 仍不能执行教师的 AI 出题审核或 AI 阅卷复核。
- [ ] 确认没有新增开发模式 API、后端免认证路径或明文测试密码。
- [ ] 若决定彻底移除功能，按“需要删除的代码”表删除 T119-T124 对应实现和文档，并重新运行认证契约测试。
