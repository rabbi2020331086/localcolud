import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api/client";
import { useAuth } from "./store/auth";
import { Layout } from "./components/Layout";
import { LoginPage, RegisterPage } from "./pages/Auth";
import { Dashboard } from "./pages/Dashboard";
import {
  InstanceDetailsPage,
  InstancesPage,
  LaunchPage,
} from "./pages/Instances";
import { MonitorPage, TerminalPage } from "./pages/Live";
import { OperationsPage } from "./pages/Operations";
import { AutoscalingPage, TemplatesPage } from "./pages/Resources";
import { LoadBalancersPage } from "./pages/LoadBalancers";
import {
  KeyPairsPage,
  AmisPage,
  SecurityGroupsPage,
  VolumesPage,
} from "./pages/CloudResources";
import type { User } from "./types";
function Protected() {
  const { accessToken, ready } = useAuth();
  const location = useLocation();
  if (!ready) return <div className="splash">Loading LocalEC2…</div>;
  return accessToken ? (
    <Layout />
  ) : (
    <Navigate to="/login" replace state={{ from: location.pathname }} />
  );
}
export default function App() {
  const { setSession, setReady } = useAuth();
  useEffect(() => {
    api
      .post<{ access_token: string; user: User }>("/auth/refresh")
      .then(({ data }) => setSession(data.access_token, data.user))
      .catch(setReady);
  }, [setReady, setSession]);
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route element={<Protected />}>
        <Route index element={<Dashboard />} />
        <Route path="instances" element={<InstancesPage />} />
        <Route path="instances/new" element={<LaunchPage />} />
        <Route path="instances/:instanceId" element={<InstanceDetailsPage />} />
        <Route
          path="instances/:instanceId/connect"
          element={<TerminalPage />}
        />
        <Route path="instances/:instanceId/monitor" element={<MonitorPage />} />
        <Route path="launch-templates" element={<TemplatesPage />} />
        <Route path="autoscaling" element={<AutoscalingPage />} />
        <Route path="autoscaling/new" element={<AutoscalingPage />} />
        <Route path="autoscaling/:groupId" element={<AutoscalingPage />} />
        <Route path="load-balancers" element={<LoadBalancersPage />} />
        <Route path="operations" element={<OperationsPage />} />
        <Route path="volumes" element={<VolumesPage />} />
        <Route path="security-groups" element={<SecurityGroupsPage />} />
        <Route path="key-pairs" element={<KeyPairsPage />} />
        <Route path="amis" element={<AmisPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
