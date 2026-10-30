import { Badge, Box, Divider, Group, Paper, Text, UnstyledButton } from '@mantine/core';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useId, useState, type ReactNode } from 'react';

export type ActivityGroupId = 'in-progress' | 'waiting' | 'attention' | 'completed';

export default function ActivityGroup({ id, title, count, actions, children }: {
    id: ActivityGroupId;
    title: string;
    count: number;
    actions?: ReactNode;
    children: ReactNode;
}) {
    const preference = 'reeldock-activity-group-' + id + '-collapsed';
    const [collapsed, setCollapsed] = useState(() => {
        try { return localStorage.getItem(preference) === 'true'; }
        catch { return false; }
    });
    const contentId = useId();
    function toggle() {
        const next = !collapsed;
        setCollapsed(next);
        try { localStorage.setItem(preference, String(next)); }
        catch { /* The group remains usable when browser storage is unavailable. */ }
    }
    const Chevron = collapsed ? ChevronRight : ChevronDown;
    return <Paper component="section" withBorder radius="md" p={0} aria-label={title + ' activity'}>
        <Group justify="space-between" align="center" wrap="wrap" gap="xs" px="sm" py="xs">
            <UnstyledButton type="button" mih={44} style={{ flex: '1 1 180px', minWidth: 0 }} onClick={toggle}
                aria-label={(collapsed ? 'Expand ' : 'Collapse ') + title + ' activity'} aria-expanded={!collapsed} aria-controls={contentId}>
                <Group component="span" gap="xs" wrap="nowrap">
                    <Chevron size={18} strokeWidth={1.8} aria-hidden="true" style={{ flexShrink: 0 }}/>
                    <Text component="span" size="sm" fw={600} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{title}</Text>
                    <Badge component="span" variant="light" color="gray" style={{ flexShrink: 0 }}>{count}</Badge>
                </Group>
            </UnstyledButton>
            {actions}
        </Group>
        <Box id={contentId} hidden={collapsed}>{!collapsed && <><Divider/>{children}</>}</Box>
    </Paper>;
}
