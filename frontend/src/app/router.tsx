import { Navigate, Route, Routes } from "react-router-dom";

import { PaperReviewPage } from "../features/paper-import/PaperReviewPage";
import { PracticeResultsRoute } from "../features/results/PracticeResultsPage";

import { AppShell } from "./AppShell";
import { DiagnosticsPage } from "../features/diagnostics/DiagnosticsPage";
import { OnboardingPage } from "../features/onboarding/OnboardingPage";
import { QuickStartPage } from "../features/onboarding/QuickStartPage";
import { SetupHomePage } from "../features/onboarding/SetupHomePage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { SubjectPage, SubjectsPage } from "../features/subjects/SubjectsPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<SubjectsPage />} />
        <Route path="/subjects/:subjectId" element={<SubjectPage />} />
        <Route path="/subjects/:subjectId/imports/:importId" element={<PaperReviewPage />} />
        <Route path="/subjects/:subjectId/practices/:practiceId" element={<PracticeResultsRoute />} />
        <Route path="/subjects/:subjectId/:area" element={<SubjectPage />} />
        <Route path="/setup/quickstart" element={<QuickStartPage />} />
        <Route path="/setup" element={<SetupHomePage />} />
        <Route path="/setup/advanced" element={<OnboardingPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/business" element={<Navigate to="/" replace />} />
        <Route path="/diagnostics" element={<DiagnosticsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
