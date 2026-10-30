import ChannelDialog from './ChannelDialog';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Box, Loader } from '@mantine/core';
import type { Bootstrap } from './AuthApp';
import { NotificationCenter } from './notifications';
import AppearanceProvider from './app/AppearanceProvider';
import '@mantine/core/styles.css';
import './app/base.css';

const AuthApp = React.lazy(() => import('./AuthApp'));
const LiveDashboard = React.lazy(() => import('./LiveDashboard'));

const initial = document.getElementById('reeldock-bootstrap');
const bootstrap: Bootstrap | null = initial ? JSON.parse(initial.textContent || '{}') : null;
createRoot(document.getElementById('root')!).render(<React.StrictMode><AppearanceProvider><React.Suspense fallback={<Box p="lg" role="status"><Loader aria-label="Loading ReelDock" /></Box>}>{bootstrap ? <AuthApp bootstrap={bootstrap} /> : <><LiveDashboard /><ChannelDialog /></>}</React.Suspense><NotificationCenter /></AppearanceProvider></React.StrictMode>);
