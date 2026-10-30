import { Box, FileInput, type FileInputProps } from '@mantine/core';
import { useRef, useState } from 'react';
import { FileCheck2, FileUp } from 'lucide-react';

function bytes(size: number) {
  if (size >= 1048576) return (size / 1048576).toFixed(1) + ' MB';
  if (size >= 1024) return Math.round(size / 1024) + ' KB';
  return size + ' B';
}

/** Single-file selection only; callers own validation, scanning and queuing. */
type DropZoneProps=Omit<FileInputProps,'value'|'defaultValue'|'onChange'|'multiple'|'resetRef'>&{
  file:File|null;onFile:(file:File|null)=>void;
};
export default function DropZone({ label, accept, file, onFile, disabled, readOnly, description, fileInputProps, placeholder='Choose file or drop it here', variant='default', ...props }: DropZoneProps) {
  const [over, setOver] = useState(false);
  const resetInput = useRef<(() => void) | null>(null);
  return <Box style={{ minWidth: 0 }}
    onDragOver={event => { event.preventDefault(); if (!disabled&&!readOnly) setOver(true); }}
    onDragLeave={() => setOver(false)}
    onDrop={event => { event.preventDefault(); event.stopPropagation(); setOver(false); const dropped = event.dataTransfer.files?.[0]; if (dropped && !disabled&&!readOnly) onFile(dropped); }}>
    <FileInput {...props} label={label} aria-label={props['aria-label']||(typeof label==='string'?label:undefined)} accept={accept} multiple={false} value={file} disabled={disabled} readOnly={readOnly}
      fileInputProps={{ ...fileInputProps, disabled }} resetRef={resetInput} clearable rightSectionWidth={44} inputWrapperOrder={['label','input','description','error']}
      clearButtonProps={{ ...props.clearButtonProps, 'aria-label': props.clearButtonProps?.['aria-label']||(file ? 'Remove ' + file.name : 'Remove selected file'), disabled:disabled||readOnly, style: { ...props.clearButtonProps?.style, width: 44, height: 44, minWidth: 44, minHeight: 44 } }}
      onChange={selected => { if (!disabled&&!readOnly) { onFile(selected); resetInput.current?.(); } }}
      variant={over && !disabled&&!readOnly ? 'filled' : variant} placeholder={placeholder}
      leftSection={file ? <FileCheck2 size={18} aria-hidden="true" /> : <FileUp size={18} aria-hidden="true" />}
      description={file?<>{description}{description?' · ':''}{bytes(file.size)} · choose or drop another file to replace it</>:description}/>
  </Box>;
}
