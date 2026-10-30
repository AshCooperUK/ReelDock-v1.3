import { Box, Group, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import type { ReactNode } from 'react';

export function PageFrame({children}:{children:ReactNode}) {
 return <Stack gap="xl" w="100%" style={{minWidth:0}}>{children}</Stack>;
}
export function PageHeader({title,description,actions}:{title:ReactNode;description?:ReactNode;actions?:ReactNode}) {
 return <Group justify="space-between" align="flex-start" gap="md" wrap="wrap"><Stack gap={4} style={{minWidth:0,flex:1}}><Title order={1} size="h2">{title}</Title>{description&&<Text c="dimmed" size="sm">{description}</Text>}</Stack>{actions&&<Group gap="xs" wrap="wrap">{actions}</Group>}</Group>;
}
export function Panel({title,description,actions,children}:{title?:ReactNode;description?:ReactNode;actions?:ReactNode;children:ReactNode}) {
 return <Paper withBorder radius="md" p="md" style={{minWidth:0}}><Stack gap="md">{(title||actions)&&<Group justify="space-between" align="flex-start" gap="md"><Box style={{minWidth:0,flex:1}}>{title&&<Title order={2} size="h4">{title}</Title>}{description&&<Text c="dimmed" size="sm" mt={4}>{description}</Text>}</Box>{actions}</Group>}{children}</Stack></Paper>;
}
export function Toolbar({children}:{children:ReactNode}) {
 return <Paper withBorder radius="md" p="sm" style={{minWidth:0}}>{children}</Paper>;
}
export function FormGrid({children,columns=2}:{children:ReactNode;columns?:number}) {
 return <SimpleGrid cols={{base:1,sm:columns}} spacing="md" verticalSpacing="lg" style={{minWidth:0}}>{children}</SimpleGrid>;
}
