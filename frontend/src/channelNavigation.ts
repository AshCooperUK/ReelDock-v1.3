export type ChannelTarget={id:string;name?:string;avatar?:string;mode?:'info'|'options'|'overview'|'settings'|'releases'|'history'|'downloads'|'manage'};
export const channelEvent='reeldock-open-channel';
export function openChannel(target:ChannelTarget){window.dispatchEvent(new CustomEvent<ChannelTarget>(channelEvent,{detail:target}));}
