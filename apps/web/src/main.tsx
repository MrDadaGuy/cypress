import { TicketDetailPage, TicketsPage } from "./admin/TicketsPage";
import OrgPage from "./admin/OrgPage";
import PeoplePage from "./admin/PeoplePage";
import ModelsPage from "./admin/ModelsPage";
import MembersPage from "./admin/MembersPage";
import PromptsPage from "./admin/PromptsPage";
import EvalsPage from "./admin/EvalsPage";
import ContentChecksPage from "./admin/ContentChecksPage";
import { ConversationDetail, ConversationsPage } from "./admin/ConversationsPage";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./styles.css";
import { installGlobalHandlers } from "./lib/report";
import { installStackTables } from "./lib/stackTables";
import AdminLayout, { AdminHome } from "./admin/AdminLayout";
import SetsPage from "./admin/SetsPage";
import SetDetail from "./admin/SetDetail";
import { AuditPage, DocumentPage, JobsPage } from "./admin/OtherPages";
import ChatPage from "./chat/ChatPage";

const router = createBrowserRouter([
  { path: "/", element: <ChatPage /> },
  {
    path: "/admin",
    element: <AdminLayout />,
    children: [
      { index: true, element: <AdminHome /> },
      { path: "sets", element: <SetsPage /> },
      { path: "sets/:id", element: <SetDetail /> },
      { path: "documents/:id", element: <DocumentPage /> },
      { path: "jobs", element: <JobsPage /> },
      { path: "audit", element: <AuditPage /> },
      { path: "conversations", element: <ConversationsPage /> },
      { path: "org", element: <OrgPage /> },
      { path: "people", element: <PeoplePage /> },
      { path: "models", element: <ModelsPage /> },
      { path: "members", element: <MembersPage /> },
      { path: "prompts", element: <PromptsPage /> },
      { path: "evals", element: <EvalsPage /> },
      { path: "checks", element: <ContentChecksPage /> },
      { path: "evals/:runId", element: <EvalsPage /> },
      { path: "evals/sub/:subId", element: <EvalsPage /> },
      { path: "tickets", element: <TicketsPage /> },
      { path: "tickets/:id", element: <TicketDetailPage /> },
      { path: "conversations/:id", element: <ConversationDetail /> },
    ],
  },
]);

installGlobalHandlers();
installStackTables();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
