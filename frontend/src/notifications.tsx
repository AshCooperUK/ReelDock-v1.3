import notificationStyles from './app/Notifications.module.css';
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Notification as MantineNotification, Group, Stack, Text } from '@mantine/core';
import { CheckCircle2, CircleAlert, Info, TriangleAlert } from 'lucide-react';
import { dismissNotification, emptyNotifications, notificationSnapshot, notify, subscribeNotifications, updateNotification, type AppNotification, type NotificationKind } from './notifications-state';

export { dismissNotification, notify } from './notifications-state';

/** Outcome notifications persist independently of the page which produced them. */
export function NotificationMessage({ message, kind = 'info', title, actions, onDismiss, id }: {
  message?: string; kind?: NotificationKind; title?: string; actions?: ReactNode;
  onDismiss?: () => void | Promise<void>; id?: string;
}) {
  const shown = useRef('');
  const callback = useRef(onDismiss);
  callback.current = onDismiss;
  const dismiss = useRef(() => callback.current?.());
  useEffect(() => {
    if (!message?.trim()) return;
    shown.current = notify({ id, message, kind, title, actions, onDismiss: onDismiss ? dismiss.current : undefined });
  }, [message, kind, title, id]);
  useEffect(() => {
    if (shown.current) updateNotification(shown.current, { actions, onDismiss: onDismiss ? dismiss.current : undefined });
  }, [actions, onDismiss]);
  return null;
}

const headings: Record<NotificationKind, string> = { success: 'Success', error: 'Error', warning: 'Attention needed', info: 'Notification' };
const icons = { success: CheckCircle2, error: CircleAlert, warning: TriangleAlert, info: Info };
const colors: Record<NotificationKind,string> = { success: 'green', error: 'red', warning: 'yellow', info: 'blue' };

function Balloon({ item }: { item: AppNotification }) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const remaining = useRef(8000);
  const Icon = icons[item.kind];
  useEffect(() => { remaining.current = 8000; }, [item.revision]);
  useEffect(() => {
    if (item.kind === 'error' || item.kind === 'warning' || item.actions || hovered || focused || item.dismissing) return;
    const started = Date.now();
    const timer = window.setTimeout(() => { void dismissNotification(item.id); }, remaining.current);
    return () => { window.clearTimeout(timer); remaining.current = Math.max(0, remaining.current - (Date.now() - started)); };
  }, [item.id, item.revision, item.kind, item.actions, item.dismissing, hovered, focused]);
  // The transparent popover viewport passes through unused space. Each real
  // notification must opt back into hit testing, including its close/actions.
  return <MantineNotification className={notificationStyles.card} style={{ pointerEvents: 'auto', flexShrink: 0 }} data-notification-id={item.id} color={colors[item.kind]} title={item.title || headings[item.kind]}
    icon={<Icon size={20} aria-hidden="true" />} role={item.kind === 'error' ? 'alert' : 'status'} aria-atomic="true"
    closeButtonProps={{ size: 44, 'aria-label': `Dismiss ${item.title || headings[item.kind]} notification`, disabled: item.dismissing }}
    onClose={() => { void dismissNotification(item.id); }}
    onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)} onFocusCapture={() => setFocused(true)} onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }}>
    <Stack gap="sm"><Text size="sm" style={{ overflowWrap: 'anywhere' }}>{item.message}</Text>{item.actions && <Group gap="xs" wrap="wrap">{item.actions}</Group>}</Stack>
  </MantineNotification>;
}

export function NotificationCenter() {
  const { items, host } = useSyncExternalStore(subscribeNotifications, notificationSnapshot, () => emptyNotifications);
  const viewport = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const remember = (event: FocusEvent) => {
      if (event.target instanceof HTMLElement && !event.target.closest('[data-notification-viewport]')) previousFocus.current = event.target;
    };
    const shortcut = (event: KeyboardEvent) => {
      if (event.key === 'F8' && viewport.current?.querySelector('button')) { event.preventDefault(); viewport.current.querySelector('button')?.focus(); }
    };
    document.addEventListener('focusin', remember);
    document.addEventListener('keydown', shortcut);
    return () => { document.removeEventListener('focusin', remember); document.removeEventListener('keydown', shortcut); };
  }, []);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element || !items.length) return;
    // A manual native popover is non-modal and escapes transformed/clipped
    // Settings surfaces, while its DOM stays within the active focus scope.
    if (typeof element.showPopover === 'function' && !element.matches(':popover-open')) element.showPopover();
  }, [host, items.length]);
  const hadFocus = useRef(false);
  useLayoutEffect(() => {
    if (!items.length && hadFocus.current && previousFocus.current?.isConnected) previousFocus.current.focus();
    hadFocus.current = !!viewport.current?.contains(document.activeElement);
  }, [items]);
  if (typeof document === 'undefined' || !items.length) return null;
  return createPortal(<div ref={viewport} popover="manual" className={notificationStyles.viewport} data-notification-viewport="" role="region" aria-label="Notifications" aria-live="polite" onPointerDown={event => event.stopPropagation()} onFocusCapture={() => { hadFocus.current = true; }} onKeyDown={event=>{if(event.key==='Escape'){const id=(event.target as HTMLElement).closest<HTMLElement>('[data-notification-id]')?.dataset.notificationId;if(id){event.preventDefault();event.stopPropagation();void dismissNotification(id);}}}}>
    {items.map(item => <Balloon key={item.id} item={item} />)}
  </div>, host || document.body);
}
