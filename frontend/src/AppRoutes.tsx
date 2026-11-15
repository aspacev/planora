import { Navigate, Route, Routes } from "react-router-dom";

import { RequireAuth } from "./auth/RequireAuth";
import { Admin } from "./screens/Admin";
import { AiIntake } from "./screens/AiIntake";
import { DocumentIntake } from "./screens/DocumentIntake";
import { ForgotPassword } from "./screens/ForgotPassword";
import { Invite } from "./screens/Invite";
import { Login } from "./screens/Login";
import { Members } from "./screens/Members";
import { MyTasks } from "./screens/MyTasks";
import { OrgSettings } from "./screens/OrgSettings";
import { Profile } from "./screens/Profile";
import { Project } from "./screens/Project";
import { ProjectSettings } from "./screens/ProjectSettings";
import { Projects } from "./screens/Projects";
import { PublicProject } from "./screens/PublicProject";
import { Register } from "./screens/Register";
import { ResetPassword } from "./screens/ResetPassword";
import { Settings, SettingsHome } from "./screens/Settings";
import { VerifyEmail } from "./screens/VerifyEmail";

/**
 * The routes live apart from `App`, because in production they are wrapped by
 * `BrowserRouter` and in tests by `MemoryRouter`: two routers in one tree are
 * incompatible, and splitting them here spares the tests from faking the history.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/register" element={<Register />} />
      <Route path="/login" element={<Login />} />
      {/* Both recovery pages live outside RequireAuth: people come here precisely
          because they have nothing to sign in with, and a link from an email is
          opened in the browser the mail arrived in — as is address confirmation. */}
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      {/* The public page lives outside RequireAuth: a guest has no session and
          never will, and the wrapper would take them to the sign-in — that is, the
          link the whole thing was for would not open at all. */}
      <Route path="/p/:orgSlug/:projectSlug" element={<PublicProject />} />
      {/* Outside RequireAuth: a link from an email is opened in the browser the
          mail arrived in, and demanding a sign-in there would mean breaking the
          most ordinary scenario — the email on a phone, the work on a laptop. */}
      <Route path="/verify-email" element={<VerifyEmail />} />
      {/* Outside RequireAuth: an invitee is not in the system yet, and sending
          them to sign in before they learn what they are being invited to means
          asking them to sign without looking. */}
      <Route path="/invite/:token" element={<Invite />} />
      <Route element={<RequireAuth />}>
        {/* The root leads to the list of projects rather than showing a screen of
            its own: that is where signing in leads, that is where the column's
            item points, and the address "/" is what people type by hand and put in
            bookmarks. A second screen with the same "Projects" heading used to
            live here, and a person arriving by the menu item ended up somewhere
            other than where the sign-in had led them — on a page no column item
            highlighted. */}
        <Route path="/" element={<Navigate to="/projects" replace />} />
        <Route path="/my-tasks" element={<MyTasks />} />
        <Route path="/projects" element={<Projects />} />
        {/* The interview is available only when creating a new project: launching
            it inside an existing one is the next stage, not the first version. */}
        <Route path="/projects/new/ai" element={<AiIntake />} />
        {/* The same intake, started from a requirements document. The session's number
            rides in the address (?session=…): reading a long document takes a while,
            and a closed tab must come back to the step it was closed on. */}
        <Route path="/projects/new/document" element={<DocumentIntake />} />
        <Route path="/projects/:projectId" element={<Project />} />
        {/* The proposal is a tab of the same screen with an address of its own, by
            the same rule as the history: a quote is discussed in conversations, and
            "open the proposal" should be a link rather than an instruction. */}
        <Route path="/projects/:projectId/proposal" element={<Project tab="proposal" />} />
        {/* The scorecard is a tab with its own address too: a weekly summary is
            sent as a link in a conversation, and it must open straight away. The
            public page (/p/...) deliberately does not have this route. */}
        <Route path="/projects/:projectId/scorecard" element={<Project tab="scorecard" />} />
        {/* The history is a tab of the same screen but with an address of its own:
            a journal entry gets linked to in conversations, and "open the project's
            history" should be a link rather than a three-step instruction. */}
        <Route path="/projects/:projectId/history" element={<Project tab="history" />} />
        {/* The project's settings get their own address rather than a dialog on top
            of the chart: they are opened rarely, for a long time and with
            discussion, and a dialog on top of what you are configuring shows the
            result by halves. */}
        <Route path="/projects/:projectId/settings" element={<ProjectSettings />} />
        {/* The workspace settings are one section with tabs rather than three
            neighbouring items in the column: the organization, the members and the
            profile all configure one and the same place of work, and the difference
            between them is a matter of level, not of different sections. */}
        <Route path="/settings" element={<Settings />}>
          <Route index element={<SettingsHome />} />
          <Route path="organization" element={<OrgSettings />} />
          <Route path="members" element={<Members />} />
          <Route path="profile" element={<Profile />} />
        </Route>
        {/* The director's panel is not a settings tab: the settings edit one
            organization's workspace, while here is a list of the whole install's
            registrations, which a person with no organization at all may be
            watching on this screen. Access is decided neither by RequireAuth nor by
            a role in an organization — the server answers 403 to anyone not
            carrying the director role (see app.director), and the screen shows that
            refusal with the same banner as any other closed route. */}
        <Route path="/admin" element={<Admin />} />
        {/* The roster's former short address: links to it have already been sent
            out, and answering them with "page not found" because of a move is a
            price for tidying up that the reader pays rather than us. */}
        <Route path="/members" element={<Navigate to="/settings/members" replace />} />
        {/* The portfolio has been taken apart: the list of projects answers "how
            are things" itself — every card carries a verdict, readiness and a date.
            Two screens with one set of projects and different completeness of data
            forced a choice of which to look at, and the answer "at both" was wrong.
            The address answers with a redirect rather than "page not found": people
            came to it from bookmarks. */}
        <Route path="/portfolio" element={<Navigate to="/projects" replace />} />
        {/* Reports have been taken apart the same way the portfolio was before:
            their table moved into "Projects" and became what that section shows its
            summary with — no second section with the same set of projects is left.
            The address answers with a redirect rather than "page not found": people
            came to it both from the column and from bookmarks. */}
        <Route path="/reports" element={<Navigate to="/projects" replace />} />
      </Route>
      {/* An unknown address leads inside, and from there to the sign-in if the
          person is not signed in. A separate "not found" screen will appear when
          there are addresses that can be confused. */}
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
