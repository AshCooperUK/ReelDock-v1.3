import type { ReactNode } from 'react';

export type NotificationKind = 'success' | 'error' | 'warning' | 'info';
export type NotificationInput = {
  message: string;
  kind?: NotificationKind;
  title?: string;
  id?: string;
  actions?: ReactNode;
  onDismiss?: () => void | Promise<void>;
};
export type AppNotification = Omit<NotificationInput, 'kind' | 'id'> & {
  id: string;
  kind: NotificationKind;
  revision: number;
  dismissing: boolean;
};
type Snapshot = { items: AppNotification[]; host: HTMLElement | null };
const listeners = new Set<() => void>();
const hosts: HTMLElement[] = [];
let sequence = 0;
let snapshot: Snapshot = { items: [], host: null };
export const emptyNotifications: Snapshot = snapshot;

function publish(items = snapshot.items) {
  snapshot = { items, host: [...hosts].reverse().find(host => host.isConnected) || null };
  listeners.forEach(listener => listener());
}
export function subscribeNotifications(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function notificationSnapshot() { return snapshot; }

export function notify(input: NotificationInput): string {
  const message = input.message.trim();
  if (!message) return '';
  const kind = input.kind || 'info';
  const same = snapshot.items.find(item => (!input.id || item.id === input.id) && item.message === message && item.kind === kind && item.title === input.title);
  if (same) {
    // Repeated polls and React StrictMode must not create or restart balloons.
    return same.id;
  }
  const id = input.id || `notification-${++sequence}`;
  const existing = snapshot.items.find(item => item.id === id);
  const item: AppNotification = { ...input, message, kind, id, revision: (existing?.revision || 0) + 1, dismissing: false };
  publish(existing ? snapshot.items.map(old => old.id === id ? item : old) : [...snapshot.items, item]);
  return id;
}

export function updateNotification(id: string, update: Pick<NotificationInput, 'actions' | 'onDismiss'>) {
  const previous = snapshot.items.find(item => item.id === id);
  if (!previous || previous.actions === update.actions && previous.onDismiss === update.onDismiss) return;
  publish(snapshot.items.map(item => item.id === id ? { ...item, ...update } : item));
}

export async function dismissNotification(id: string, runCallback = true): Promise<boolean> {
  const item = snapshot.items.find(item => item.id === id);
  if (!item || item.dismissing) return false;
  if (runCallback && item.onDismiss) {
    publish(snapshot.items.map(row => row.id === id ? { ...row, dismissing: true } : row));
    try { await item.onDismiss(); }
    catch (error) {
      publish(snapshot.items.map(row => row.id === id ? { ...row, dismissing: false } : row));
      notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not dismiss this notification. Please try again.' });
      return false;
    }
  }
  publish(snapshot.items.filter(row => row.id !== id));
  return true;
}

export function registerNotificationHost(host: HTMLElement) {
  hosts.push(host);
  publish();
  return () => {
    const index = hosts.lastIndexOf(host);
    if (index >= 0) hosts.splice(index, 1);
    publish();
  };
}
