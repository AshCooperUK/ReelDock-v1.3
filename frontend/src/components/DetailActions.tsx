import { Box, Group, Stack } from '@mantine/core';
import type { ReactNode } from 'react';

/** Metadata may wrap; the Favourite/Watchlist/Quick icon row stays together. */
export default function DetailActions({metadata,feedback,children}:{metadata?:ReactNode;feedback?:ReactNode;children:ReactNode}){
 return <Group align="flex-start" gap="xs" wrap="wrap" style={{minWidth:0,maxWidth:'100%'}}>
  {metadata&&<Box data-detail-action-metadata style={{minWidth:0,flexShrink:0}}>{metadata}</Box>}
  <Stack data-detail-action-unit gap="xs" style={{minWidth:0,maxWidth:'100%',flexShrink:0}}>
   <Group data-detail-action-icons align="flex-start" gap="xs" wrap="nowrap">{children}</Group>
   {feedback}
  </Stack>
 </Group>;
}
