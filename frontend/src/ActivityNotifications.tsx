import { NotificationMessage, dismissNotification } from './notifications';
import { api } from './api';
import ChannelIdentity from './ChannelIdentity';
import { useEffect, useState } from 'react';
import { Group } from '@mantine/core';
import { Check, X } from 'lucide-react';
import { Button } from './components/ui';
import { useDownloadFeed, refreshDownloads, type ActivityAlert } from './downloadFeed';
export function AlertTile({ alert, onCookies, csrf, local }: {
    alert: ActivityAlert;
    onCookies?: () => void;
    csrf: string;
    local?: boolean;
}) {
    const [dismissed, setDismissed] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState('');
    const notificationId = 'activity-alert:' + alert.id;
    useEffect(() => () => { void dismissNotification(notificationId, false); }, [notificationId]);
    async function finish() { setDismissed(true); await dismissNotification(notificationId, false); }
    async function restore(accept: boolean) { setSaving(true); setError(''); try {
        await api('/api/activity/subscription-return', undefined, { channel: alert.channel?.id, subscription: alert.subscription, accept }, csrf);
        await finish();
        void refreshDownloads();
        window.dispatchEvent(new Event('reeldock-library-changed'));
    }
    catch {
        setError('Could not update this channel. Please try again.');
    }
    finally {
        setSaving(false);
    } }
    async function retry() { if (!alert.retry)
        return; setSaving(true); setError(''); try {
        await api('/api/downloads', undefined, { id: alert.retry.id, action: 'retry' }, csrf);
        await refreshDownloads();
        await finish();
    }
    catch (exc) {
        setError(exc instanceof Error ? exc.message : 'Could not retry this download.');
    }
    finally {
        setSaving(false);
    } }
    async function dismiss() { if (local) {
        setDismissed(true);
        return;
    } setSaving(true); setError(''); try {
        await api('/api/activity/dismiss', undefined, { id: alert.id }, csrf);
        setDismissed(true);
        void refreshDownloads();
    }
    catch {
        throw new Error('Could not dismiss this alert. Please try again.');
    }
    finally {
        setSaving(false);
    } }
    if (dismissed)
        return null;
    const subscription = !!(alert.channel && alert.subscription);
    return <><NotificationMessage id={notificationId} kind={subscription || onCookies ? 'warning' : 'error'} title={alert.title} message={alert.message} onDismiss={dismiss} actions={<>
  {subscription && <><ChannelIdentity {...alert.channel!}/><Group gap="xs"><Button variant="secondary" aria-label={'Restore ' + alert.channel!.name} disabled={saving} onClick={() => void restore(true)}><Check size={16}/>Add back</Button><Button variant="secondary" aria-label={'Keep ' + alert.channel!.name + ' removed'} disabled={saving} onClick={() => void restore(false)}><X size={16}/>Keep removed</Button></Group></>}
  {alert.retry && <Button variant="secondary" disabled={saving} onClick={() => void retry()}>Retry</Button>}
  {onCookies && <Button variant="secondary" disabled={saving} onClick={onCookies}>Update cookies</Button>}
 </>}/><NotificationMessage kind="error" message={error}/></>;
}
/** Activity alerts are independent of whether overview widgets are visible. */
export function ActivityNotifications({ csrf }: {
    csrf: string;
}) {
    const { activity: data, error } = useDownloadFeed();
    return <>
  {error && <AlertTile csrf={csrf} local key={error} alert={{ id: 'activity-feed-' + error, title: 'Activity unavailable', message: error }}/>}
  {(data?.alerts || []).map(alert => <AlertTile csrf={csrf} key={alert.id} alert={alert}/>)}
 </>;
}
