# 开发模式快速登录

开发模式快速登录只用于 EduAgent 的本地演示和测试。它让演示者可以在登录页直接选择一个角色，减少反复输入测试凭证的步骤；它不改变后端认证和授权边界。

## 开关

在 `.env` 中设置：

```dotenv
DEV_MODE=false
```

`DEV_MODE` 的默认值为 `false`。配置从 `.env` 读取，应用重启后生效。

- `DEV_MODE=false`：登录页只显示用户名/邮箱和密码输入，行为与普通登录完全一致。
- `DEV_MODE=true`：登录页顶部显示“开发模式快速登录”区域，工作台顶部显示“当前为开发模式，请勿用于生产环境”。

开发环境也必须配置有效的 `JWT_SECRET_KEY`。快速登录不会使用固定令牌，也不会把用户标识直接当作会话凭证。

## 预设账号生命周期

开启开发模式后，`ensure_dev_mode_accounts()` 按以下固定标识幂等准备账号：

| 角色 | 用户名 | 邮箱 |
| :--- | :--- | :--- |
| 管理员 | `dev_admin` | `dev_admin@eduagent.local` |
| 教师 | `dev_teacher` | `dev_teacher@eduagent.local` |
| 学生 | `dev_student` | `dev_student@eduagent.local` |

每个账号只绑定对应的一个角色并保持启用。账号不存在时生成随机密码并只保存密码哈希；快速登录不需要知道这些密码。重复启动或重复点击按钮不会重复创建账号。账号标识发生冲突时应停止初始化并报告冲突，不能覆盖已有真实账号的资料。

关闭 `DEV_MODE` 只会停止后续初始化和隐藏快速登录区域，不会自动删除已经创建的账号，也不会撤销已经签发的令牌。测试完成后应删除或停用 `dev_admin`、`dev_teacher`、`dev_student`；需要立即使旧令牌全部失效时，同时轮换 `JWT_SECRET_KEY`。

## 快速登录行为

登录页提供三个按钮：

- “以管理员身份登录”
- “以教师身份登录”
- “以学生身份登录”

点击按钮后，Gradio UI 会在开发模式下准备对应账号，再调用现有 `AuthService.issue_access_token()` 签发标准 JWT，并把 token、用户 ID 和角色写入当前浏览器会话的 `LoginState`。随后按该账号的角色显示导航。

快速登录只跳过 UI 的密码输入。每次业务操作仍会重新验证 JWT、读取数据库中的用户和启用状态，并执行角色/权限检查。管理员账号仍不能访问教师的 AI 出题审核或 AI 阅卷复核功能。

## 后端安全边界

受保护请求继续经过以下链路：

```text
Bearer JWT
  -> JWT 签名、类型和有效期校验
  -> 按 sub 从数据库加载用户并检查启用状态
  -> RoleGuard / PermissionGuard
  -> 业务 Service
```

`/api/auth/login`、`/api/auth/me`、`core/security.py` 中的认证依赖和 RBAC 守卫没有开发模式旁路。前端按钮的可见性只用于改善本地操作体验，不能作为 API 的权限判断。

## 本地验证步骤

1. 复制 `.env.example` 为 `.env`，填写数据库、JWT 和其他运行配置。
2. 将 `DEV_MODE` 设为 `true` 并重启应用。
3. 打开 Gradio 登录页，确认三个快速登录按钮和开发模式横幅出现。
4. 分别点击三个按钮，确认当前会话角色、导航和可访问页面符合 Admin、Teacher、Student 边界。
5. 使用后端 API 或现有认证契约测试验证 JWT、停用账号和越权请求仍被拒绝。
6. 将 `DEV_MODE` 改回 `false` 并重启，确认快速登录区域和横幅消失，用户名/密码登录仍可用。

## 上线前清理

上线前必须完成 [`dev-mode-removal-checklist.md`](dev-mode-removal-checklist.md) 中的删除、配置关闭、账号清理和认证回归检查。至少确认：

- 生产环境 `DEV_MODE=false`；
- 三个预设账号已删除或停用；
- 登录页不再显示快速登录区域；
- 后端 JWT、当前用户加载和 RBAC 守卫仍然保留并通过测试。
