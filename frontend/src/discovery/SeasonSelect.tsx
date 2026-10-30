import { SelectField } from '../components/SelectField';

/** Use the shared styled dropdown, viewport bounds and first-Escape handling. */
export default function SeasonSelect({seasons,value,onChange}:{seasons:number[];value:string;onChange:(value:string)=>void}) {
 return <SelectField aria-label="TV season" value={value} onChange={event=>onChange(event.currentTarget.value)}
  data={[{value:'',label:'All seasons'},...seasons.map(n=>({value:String(n),label:n===0?'Specials':'Season '+n}))]}/>;
}
