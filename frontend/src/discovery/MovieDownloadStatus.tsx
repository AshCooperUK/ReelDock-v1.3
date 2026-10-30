import type { ReactNode } from 'react';
import { Group, Paper, Progress, Stack, Text } from '@mantine/core';
import { Check } from 'lucide-react';
export type MovieTransfer={file:boolean;status:string;requestState:string;progress?:number;bytes?:number;estimatedBytes?:number;filename?:string};
function size(bytes:number){return bytes>=1073741824?(bytes/1073741824).toFixed(1)+' GB':(bytes/1048576).toFixed(1)+' MB';}
export default function MovieDownloadStatus({row,action}:{row:MovieTransfer;action:ReactNode}){
 const progress=typeof row.progress==='number'&&Number.isFinite(row.progress)?Math.max(0,Math.min(100,row.progress)):0;
 const known=row.requestState==='downloading'||progress>0;
 return <Paper withBorder p="md" radius="md"><Group align="center" justify="space-between"><Stack gap="sm" style={{flex:1,minWidth:0}}><Group justify="space-between" gap="xs" role="status"><Text fw={600} size="sm">{row.file?<><Check size={16}/> Downloaded</>:row.status}</Text><Text size="xs" c="dimmed">{row.file?(row.bytes?size(row.bytes):''):progress===100?'Download received':known?Math.round(progress)+'% downloaded':''}</Text></Group>{!row.file&&(known?<Progress aria-label="Movie download progress" value={progress}/>:<Progress.Root role="progressbar" aria-label="Movie download progress"><Progress.Section withAria={false} value={100} animated/></Progress.Root>)}<Text size="xs" c="dimmed" style={{overflowWrap:'anywhere'}} title={row.file?row.filename:'Estimate from the selected release or NZB articles; the final video size may differ.'}>{row.file?row.filename:row.estimatedBytes?'Estimated download size: '+size(row.estimatedBytes):'Download size not available yet'}</Text></Stack>{!row.file&&action}</Group></Paper>;
}
