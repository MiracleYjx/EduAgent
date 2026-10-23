import { Layout, Menu, Space, Tag, Typography } from "antd";
import type { MenuProps } from "antd";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { useSettingsSession } from "../auth/SettingsSession";

const navigationItems: MenuProps["items"] = [
  { key: "/", label: "我的科目" },
  { key: "/setup/quickstart", label: "首次设置" },
  { key: "/settings", label: "设置概览" },
  { key: "/diagnostics", label: "基础诊断" },
];

const sessionLabels = {
  checking: { color: "processing", text: "正在验证管理会话" },
  authorized: { color: "success", text: "管理会话已授权" },
  unauthorized: { color: "warning", text: "需要重新授权" },
} as const;

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const session = useSettingsSession();
  const selectedKey =
    ["/setup/quickstart", "/setup", "/settings", "/diagnostics", "/business"].find((path) =>
      location.pathname.startsWith(path),
    ) ?? "/";
  const sessionLabel = sessionLabels[session.status];

  return (
    <Layout className="app-shell">
      <Layout.Header
        style={{
          height: "auto",
          padding: "16px 24px",
          background: "#ffffff",
          borderBottom: "1px solid #e5e7eb",
        }}
      >
        <Space
          style={{
            width: "100%",
            maxWidth: 1180,
            margin: "0 auto",
            justifyContent: "space-between",
          }}
          align="center"
          wrap
        >
          <Space orientation="vertical" size={0}>
            <Typography.Title level={4} style={{ margin: 0 }}>
              EduAgent
            </Typography.Title>
            <Typography.Text type="secondary">
              个人学习空间
            </Typography.Text>
          </Space>
          {selectedKey !== "/" ? <Tag color={sessionLabel.color}>{sessionLabel.text}</Tag> : null}
        </Space>
      </Layout.Header>
      <Layout>
        <Layout.Content>
          <div className="app-content">
            <div className="page-stack">
              <Menu
                mode="horizontal"
                selectedKeys={[selectedKey]}
                items={navigationItems}
                onClick={({ key }) => navigate(key)}
                style={{ background: "transparent" }}
              />
              <Outlet />
            </div>
          </div>
        </Layout.Content>
      </Layout>
      <Layout.Footer style={{ textAlign: "center", color: "#6b7280" }}>
        个人学习无需登录；设置管理使用独立授权，令牌仅保存在当前页面内存中。
      </Layout.Footer>
    </Layout>
  );
}
