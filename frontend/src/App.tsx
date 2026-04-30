import { lazy } from 'react';
import { Route, Routes } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import Layout from './components/Layout';
import ProtectedRoute from './components/auth/ProtectedRoute';
import Login from './pages/Login';

const Analytics = lazy(() => import('./pages/Analytics'));
const DeviceDetail = lazy(() => import('./pages/DeviceDetail'));
const DeviceGroupDetail = lazy(() => import('./pages/DeviceGroupDetail'));
const DeviceGroups = lazy(() => import('./pages/DeviceGroups'));
const Devices = lazy(() => import('./pages/Devices'));
const DriverDetail = lazy(() => import('./pages/DriverDetail'));
const Drivers = lazy(() => import('./pages/Drivers'));
const HostDetail = lazy(() => import('./pages/HostDetail'));
const Hosts = lazy(() => import('./pages/Hosts'));
const NotFound = lazy(() => import('./pages/NotFound'));
const Notifications = lazy(() => import('./pages/Notifications'));
const RunDetail = lazy(() => import('./pages/RunDetail'));
const Runs = lazy(() => import('./pages/Runs'));
const Sessions = lazy(() => import('./pages/Sessions'));
const Settings = lazy(() => import('./pages/Settings'));

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="devices" element={<Devices />} />
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
