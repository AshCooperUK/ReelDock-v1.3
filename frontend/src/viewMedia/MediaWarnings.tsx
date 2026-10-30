import { useState } from 'react';
import { Alert, Text } from '@mantine/core';

export default function MediaWarnings({messages=[]}:{messages?:string[]}) {
  const [dismissed,setDismissed]=useState('');
  const signature=JSON.stringify(messages);
  if(!messages.length||dismissed===signature)return null;
  return <Alert color="yellow" variant="light" title="Some sources need attention" role="status" aria-live="polite"
    withCloseButton closeButtonLabel="Dismiss source warnings" onClose={()=>setDismissed(signature)}
    styles={{closeButton:{width:44,height:44,flexShrink:0}}}>
    <Text size="sm" style={{whiteSpace:'pre-line'}}>{messages.join('\n')}</Text>
  </Alert>;
}
