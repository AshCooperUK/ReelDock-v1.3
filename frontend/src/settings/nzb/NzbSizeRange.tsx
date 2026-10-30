import { Group, RangeSlider, SimpleGrid, Stack, Text } from '@mantine/core';
import { useRef, useState } from 'react';
import { Field } from '../../SettingsUI';
import { TextInput } from '../../TextField';

const MB=1_000_000,ceiling=100_000;
export const sizeLabel=(bytes:number)=>bytes>=1_000_000_000?(bytes/1_000_000_000).toLocaleString('en-GB',{maximumFractionDigits:3})+' GB':(bytes/MB).toLocaleString('en-GB')+' MB';
export const sizePosition=(bytes:number)=>Math.log1p(bytes/MB)/Math.log1p(ceiling)*1000;
export const positionSize=(position:number)=>Math.round(Math.expm1(position/1000*Math.log1p(ceiling)))*MB;
export default function NzbSizeRange({minimum=0,maximum=ceiling*MB,disabled,save,engine='NZB'}:{engine?:string;minimum:number;maximum:number;disabled:boolean;save:(patch:{min_download_bytes:number;max_download_bytes:number})=>void}) {
  const [range,setRange]=useState<[number,number]>([minimum,maximum]),submitted=useRef([minimum,maximum]);
  function update(next:[number,number],commit=false) {
    setRange(next);
    if(commit&&(next[0]!==submitted.current[0]||next[1]!==submitted.current[1])){submitted.current=next;save({min_download_bytes:next[0],max_download_bytes:next[1]});}
  }
  function change(index:number,value:number,commit=false) {
    const next:[number,number]=[...range];next[index]=Math.max(index===0?0:range[0],Math.min(index===0?range[1]:ceiling*MB,value));update(next,commit);
  }
  function fromTrack(value:[number,number],commit=false) {update([positionSize(value[0]),positionSize(value[1])],commit);}
  return <Stack gap="md"><Stack gap={4}><Text fw={500} size="sm">Download size</Text><Text size="sm" c="dimmed">{engine==='VOD'?'Video file size. Checked before publication, including sources without a supplied file size. Active transfers finish with the limits they started with.':'Total selected NZB payload size. Applies to new downloads; active transfers finish. MB and GB use decimal units.'}</Text></Stack>
    <Group justify="space-between"><Text size="sm">{sizeLabel(range[0])}</Text><Text size="sm">{sizeLabel(range[1])}</Text></Group>
    <RangeSlider min={0} max={1000} step={1} minRange={0} pushOnOverlap={false} value={[sizePosition(range[0]),sizePosition(range[1])]} disabled={disabled} label={position=>sizeLabel(positionSize(position))} thumbSize={24} thumbFromLabel={'Minimum '+engine+' download size'} thumbToLabel={'Maximum '+engine+' download size'} thumbValueText={position=>sizeLabel(positionSize(position))} onChange={value=>fromTrack(value)} onChangeEnd={value=>fromTrack(value,true)}/>
    <SimpleGrid cols={{base:1,sm:2}} spacing="md">{(['Minimum','Maximum'] as const).map((name,index)=><Field key={name} label={name+' (MB)'}><TextInput aria-label={name+' '+engine+' size (MB)'} type="number" min={index===0?0:range[0]/MB} max={index===0?range[1]/MB:ceiling} step="1" required disabled={disabled} key={range[index]} defaultValue={range[index]/MB} onBlur={event=>{if(!event.currentTarget.checkValidity()){event.currentTarget.reportValidity();return;}change(index,Number(event.currentTarget.value)*MB,true);}}/></Field>)}</SimpleGrid>
  </Stack>;
}
