import { ActionIcon, Loader, Text, Tooltip } from '@mantine/core';
import { ArrowDownToLine } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from './api';
import DetailActions from './components/DetailActions';

export type QuickDownloadResult={state:'queued'|'searching'|'complete'|'choice_required';message:string;requestId?:string;downloadAll?:boolean};

/** One explicit header action. Settings and acquisition policy belong to the server. */
export default function QuickDownload({item,csrf,onChoices,navigateOnly=false,disabled=false,disabledReason,readOnly=false,actions,metadata}:{
 item:object;csrf:string;onChoices:()=>void;navigateOnly?:boolean;disabled?:boolean;disabledReason?:string;readOnly?:boolean;
 actions?:ReactNode;metadata?:ReactNode;
}){
 const target=item as {kind?:unknown;id?:unknown;videoId?:unknown;externalId?:unknown;source?:unknown;path?:unknown};
 const key=JSON.stringify([target.kind,target.id,target.videoId,target.externalId,target.source,target.path,csrf]);
 const identity=useRef(key),request=useRef<AbortController|null>(null);
 identity.current=key;
 const[feedback,setFeedback]=useState<{key:string;busy:boolean;notice:string;error:string;outcome:QuickDownloadResult['state']|null}>({key,busy:false,notice:'',error:'',outcome:null});
 const{busy,notice,error,outcome}=feedback.key===key?feedback:{busy:false,notice:'',error:'',outcome:null};
 useEffect(()=>()=>{request.current?.abort();request.current=null;},[key]);
 const locked=disabled||readOnly||(!navigateOnly&&!csrf)||outcome==='queued'||outcome==='searching'||outcome==='complete';
 async function download(){
  if(locked||request.current)return;
  if(navigateOnly||outcome==='choice_required'){onChoices();return;}
  const controller=new AbortController(),owner=key;request.current=controller;setFeedback({key:owner,busy:true,notice:'',error:'',outcome:null});
  try{
   const result=await api<QuickDownloadResult>('/api/quick-download',controller.signal,{item},csrf,{feedback:'inline'});
   if(controller.signal.aborted||request.current!==controller||identity.current!==owner)return;
   if(!result||!['queued','searching','complete','choice_required'].includes(result.state)||typeof result.message!=='string')throw new Error('The download could not be confirmed. Check Downloads before trying again.');
   setFeedback({key:owner,busy:false,notice:result.message,error:'',outcome:result.state});
   if(target.kind==='show'&&result.downloadAll===true&&result.state!=='choice_required')window.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:target.id,field:'downloadAll'}}));
   if(result.state==='choice_required')onChoices();
  }catch(problem){
   if(!controller.signal.aborted&&request.current===controller&&identity.current===owner)setFeedback({key:owner,busy:false,notice:'',error:problem instanceof Error?problem.message:'The download could not be requested.',outcome:null});
  }finally{
   if(request.current===controller){request.current=null;if(identity.current===owner)setFeedback(previous=>previous.key===owner?{...previous,busy:false}:previous);}
  }
 }
 const label=navigateOnly?'Choose an episode to download':target.kind==='show'?'Download all seasons with saved settings':'Quick download with saved settings';
 return <DetailActions metadata={metadata} feedback={<>
  {notice&&<Text size="sm" c="dimmed" role="status" style={{overflowWrap:'anywhere'}}>{notice}</Text>}
  {error&&<Text size="sm" c="red" role="alert" style={{overflowWrap:'anywhere'}}>{error}</Text>}
 </>}>
  {actions}
  <Tooltip label={readOnly?'Unavailable in this preview':disabledReason||notice||label}>
   <ActionIcon type="button" variant="default" size={44} miw={44} mih={44} aria-label={label} aria-busy={busy} disabled={locked||busy} onClick={()=>void download()}>
    {busy?<Loader size={18} aria-label="Requesting download"/>:<ArrowDownToLine size={18} aria-hidden="true"/>}
   </ActionIcon>
  </Tooltip>
 </DetailActions>;
}
