"use client";

import { PageSubnavContext } from "@/components/layout/page-header";
import { SettingsTabs } from "@/components/layout/settings-tabs";

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <PageSubnavContext.Provider value={<SettingsTabs />}>
      {children}
    </PageSubnavContext.Provider>
  );
}
