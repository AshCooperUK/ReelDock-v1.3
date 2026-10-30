import { useEffect, useState } from 'react';
import { Group, Loader, Text } from '@mantine/core';

/** Fast reads stay quiet; a real wait gets one small, accessible status. */
export default function LoadingStatus({ active = true, label = 'Loading page…' }: { active?: boolean; label?: string }) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    setVisible(false);
    if (!active) return;
    const timer = setTimeout(() => setVisible(true), 180);
    return () => clearTimeout(timer);
  }, [active]);
  return active && visible ? <Group role="status" aria-label={label} gap="sm" py="sm"><Loader size="xs"/><Text c="dimmed" size="sm">{label}</Text></Group> : null;
}
