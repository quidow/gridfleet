import { lazy } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Dashboard } from './pages/Dashboard';
import { Layout } from './components/Layout';
import { ProtectedRoute } from './components/auth/ProtectedRoute';
import { Login } from './pages/Login';

const Analytics = lazy(() => import('./pages/Analytics').then((m) => ({ default: m.Analytics })));
const DeviceDetail = lazy(() => import('./pages/DeviceDetail').then((m) => ({ default: m.DeviceDetail })));
const DeviceGroupDetail = lazy(() => import('./pages/DeviceGroupDetail').then((m) => ({ default: m.DeviceGroupDetail })));
const DeviceGroups = lazy(() => import('./pages/DeviceGroups').then((m) => ({ default: m.DeviceGroups })));
const Devices = lazy(() => import('./pages/Devices').then((m) => ({ default: m.Devices })));
const DriverDetail = lazy(() => import('./pages/DriverDetail').then((m) => ({ default: m.DriverDetail })));
const Drivers = lazy(() => import('./pages/Drivers').then((m) => ({ default: m.Drivers })));
const HostDetail = lazy(() => import('./pages/HostDetail').then((m) => ({ default: m.HostDetail })));
const Hosts = lazy(() => import('./pages/Hosts').then((m) => ({ default: m.Hosts })));
const NotFound = lazy(() => import('./pages/NotFound').then((m) => ({ default: m.NotFound })));
const Notifications = lazy(() => import('./pages/Notifications').then((m) => ({ default: m.Notifications })));
const RunDetail = lazy(() => import('./pages/RunDetail').then((m) => ({ default: m.RunDetail })));
const Runs = lazy(() => import('./pages/Runs').then((m) => ({ default: m.Runs })));
const Sessions = lazy(() => import('./pages/Sessions').then((m) => ({ default: m.Sessions })));
const Settings = lazy(() => import('./pages/Settings').then((m) => ({ default: m.Settings })));

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="devices" element={<Devices />} />
          <Route path="devices/import" element={<Navigate to="/settings?tab=backup" replace />} />
          <Route path="devices/:id" element={<DeviceDetail />} />
          <Route path="hosts" element={<Hosts />} />
          <Route path="hosts/:id" element={<HostDetail />} />
          <Route path="sessions" element={<Sessions />} />
          <Route path="runs" element={<Runs />} />
          <Route path="runs/:id" element={<RunDetail />} />
          <Route path="analytics" element={<Analytics />} />
          <Route path="notifications" element={<Notifications />} />
          <Route path="groups" element={<DeviceGroups />} />
          <Route path="groups/:id" element={<DeviceGroupDetail />} />
          <Route path="drivers" element={<Drivers />} />
          <Route path="drivers/:id" element={<DriverDetail />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
