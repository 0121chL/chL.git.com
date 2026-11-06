import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "安全监测 Agent 看板",
  description: "MQTT → 规则引擎 → LangGraph → 人工审批 → 工单",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
