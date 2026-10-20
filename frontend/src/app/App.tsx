import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { BrowserRouter } from "react-router-dom";

import { SettingsSessionProvider } from "../auth/SettingsSession";
import { AppRoutes } from "./router";

export function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#2563eb",
          borderRadius: 10,
          colorBgLayout: "#f4f6f9",
        },
      }}
    >
      <SettingsSessionProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </SettingsSessionProvider>
    </ConfigProvider>
  );
}
