/* eslint-disable react-refresh/only-export-components -- routes manifest mixes lazy components and the route-tree config */
import { lazy } from 'react';
import { Navigate, Outlet } from 'react-router-dom';
import type { RouteObject } from 'react-router-dom';
import { AuthProvider } from './context/AuthContext';
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
const RouterPage = lazy(() => import('./pages/Router').then((m) => ({ default: m.RouterPage })));
const NotFound = lazy(() => import('./pages/NotFound').then((m) => ({ default: m.NotFound })));
const Notifications = lazy(() => import('./pages/Notifications').then((m) => ({ default: m.Notifications })));
const RunDetail = lazy(() => import('./pages/RunDetail').then((m) => ({ default: m.RunDetail })));
const Runs = lazy(() => import('./pages/Runs').then((m) => ({ default: m.Runs })));
const Sessions = lazy(() => import('./pages/Sessions').then((m) => ({ default: m.Sessions })));
const Settings = lazy(() => import('./pages/Settings').then((m) => ({ default: m.Settings })));

export const routes: RouteObject[] = [
  {
    element: <AuthProvider><Outlet /></AuthProvider>,
    children: [
      { path: '/login', element: <Login /> },
      {
        element: <ProtectedRoute />,
        children: [
          {
            element: <Layout />,
            children: [
              { index: true, element: <Dashboard /> },
              { path: 'devices', element: <Devices /> },
              { path: 'devices/import', element: <Navigate to="/settings?tab=backup" replace /> },
              { path: 'devices/:id', element: <DeviceDetail /> },
              { path: 'hosts', element: <Hosts /> },
              { path: 'hosts/:id', element: <HostDetail /> },
              { path: 'router', element: <RouterPage /> },
              { path: 'sessions', element: <Sessions /> },
              { path: 'runs', element: <Runs /> },
              { path: 'runs/:id', element: <RunDetail /> },
              { path: 'analytics', element: <Analytics /> },
              { path: 'notifications', element: <Notifications /> },
              { path: 'groups', element: <DeviceGroups /> },
              { path: 'groups/:id', element: <DeviceGroupDetail /> },
              { path: 'drivers', element: <Drivers /> },
              { path: 'drivers/:id', element: <DriverDetail /> },
              { path: 'settings', element: <Settings /> },
              { path: '*', element: <NotFound /> },
            ],
          },
        ],
      },
    ],
  },
];
