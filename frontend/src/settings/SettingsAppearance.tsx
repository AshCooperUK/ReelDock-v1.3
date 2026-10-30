import { Box, ColorSwatch, Group, Paper, SegmentedControl, SimpleGrid, Stack, Switch, Text, ThemeIcon, UnstyledButton } from '@mantine/core';
import { Check } from 'lucide-react';
import { Panel } from '../components/layout';
import { ACCENTS } from '../app/appearance';
import { useAppearance } from '../app/AppearanceProvider';

export default function SettingsAppearance() {
 const {appearance,setAppearance}=useAppearance();
 const selected=ACCENTS.find(accent=>accent.value===appearance.accent);
 return <Stack gap="lg">
  <Panel title="Colour mode" description="Choose the appearance that is most comfortable for you.">
   <SegmentedControl role="radiogroup" aria-label="Colour mode" name="appearance-scheme" value={appearance.scheme} fullWidth size="md" radius="md" data={[{value:'light',label:'Light'},{value:'dark',label:'Dark'}]} onChange={scheme=>{if(scheme==='light'||scheme==='dark')setAppearance({...appearance,scheme});}} styles={{label:{minHeight:44,display:'flex',alignItems:'center',justifyContent:'center'}}}/>
  </Panel>
  <Panel title="Background">
   <Switch label="Background image" description="Show a subtle background that follows your selected colour palette." size="md" checked={appearance.background!==false} onChange={event=>setAppearance({...appearance,background:event.currentTarget.checked})} styles={{body:{minHeight:44,alignItems:'center'}}}/>
  </Panel>
  <Panel title="Colour palette" description="Every palette has an optional matching background in light and dark mode. Changes apply immediately.">
   <SimpleGrid cols={{base:1,xs:2,lg:3}} spacing="sm">
    {ACCENTS.map(accent=>{const active=accent.value===appearance.accent;return <UnstyledButton key={accent.value} type="button" aria-label={'Use '+accent.label+' palette'} aria-pressed={active} w="100%" mih={80} onClick={()=>setAppearance({...appearance,accent:accent.value})}>
     <Paper withBorder radius="md" p="sm" style={{borderColor:active?'var(--mantine-primary-color-filled)':undefined}}>
      <Group wrap="nowrap" gap="sm" justify="space-between">
       <Stack gap={8} style={{flex:1,minWidth:0}}><Text fw={600}>{accent.label}</Text><Group gap={4} wrap="nowrap" aria-hidden="true">{[3,4,5,6,7].map(shade=><ColorSwatch key={shade} size={18} color={`var(--mantine-color-${accent.value}-${shade})`}/>)}</Group></Stack>
       {active?<Stack align="center" gap={2} w={52} style={{flexShrink:0}}><ThemeIcon variant="light" size={28} radius="xl"><Check size={17} aria-hidden="true"/></ThemeIcon><Text size="xs" c="dimmed">Selected</Text></Stack>:<Box w={52} aria-hidden="true" style={{flexShrink:0}}/>}
      </Group>
     </Paper>
    </UnstyledButton>;})}
   </SimpleGrid>
  </Panel>
  <Text size="sm" c="dimmed" role="status" aria-live="polite">{appearance.scheme==='light'?'Light':'Dark'} · {selected?.label||appearance.accent}. Saved in this browser for this ReelDock address.</Text>
 </Stack>;
}
