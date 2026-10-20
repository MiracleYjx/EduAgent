import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./styles.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("缺少设置界面根节点。");
}

createRoot(container).render(<App />);
