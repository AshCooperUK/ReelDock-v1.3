export type ReleaseLabel={title:string;size:number;reason:string};
export function fullReleaseLabel(release:ReleaseLabel){
 return release.title+(release.size?' · '+(release.size/1073741824).toFixed(1)+' GB':'')+(release.reason?' · '+release.reason:'');
}
