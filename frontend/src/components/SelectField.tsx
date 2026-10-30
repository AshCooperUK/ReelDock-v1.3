import { CheckIcon, Group, Select, type ComboboxItem, type NativeSelectProps, type SelectProps } from '@mantine/core';
import { Children, Fragment, forwardRef, isValidElement, useEffect, useImperativeHandle, useMemo, useRef, useState, type ChangeEvent, type KeyboardEvent, type ReactNode } from 'react';

type Choice = ComboboxItem & { title?: string; ariaLabel?: string; selected?: boolean };
type ChoiceGroup = { group: string; items: Choice[] };
export type SelectFieldProps = NativeSelectProps & { readOnly?: boolean };
const text = (node: ReactNode): string => Children.toArray(node).map(child => typeof child === 'string' || typeof child === 'number' ? String(child) : isValidElement<{ children?: ReactNode }>(child) ? text(child.props.children) : '').join('').replace(/\s+/g, ' ').trim();
const scalar = (value: unknown) => value == null ? '' : String(Array.isArray(value) ? value[0] ?? '' : value);

/** Preserve native option values/labels, including fragments and disabled groups. */
export function normalizeSelectData({ children, data }: Pick<SelectFieldProps, 'children' | 'data'>): (Choice | ChoiceGroup)[] {
  const fromChildren = (nodes: ReactNode, disabled = false): (Choice | ChoiceGroup)[] => {
    const result: (Choice | ChoiceGroup)[] = [];
    Children.forEach(nodes, child => {
      if (!isValidElement<Record<string, unknown>>(child)) return;
      const props = child.props;
      if (child.type === Fragment) result.push(...fromChildren(props.children as ReactNode, disabled));
      else if (child.type === 'optgroup') result.push({ group: scalar(props.label), items: fromChildren(props.children as ReactNode, disabled || !!props.disabled).flatMap(item => 'group' in item ? item.items : [item]) });
      else if (child.type === 'option') result.push({ value: scalar(props.value ?? text(props.children as ReactNode)), label: scalar(props.label ?? text(props.children as ReactNode)), disabled: disabled || !!props.disabled, title: typeof props.title === 'string' ? props.title : undefined, ariaLabel: typeof props['aria-label'] === 'string' ? props['aria-label'] : undefined, selected: !!props.selected });
    });
    return result;
  };
  const fromData = (items: unknown[], disabled = false): (Choice | ChoiceGroup)[] => items.flatMap<Choice | ChoiceGroup>(item => {
    if (item && typeof item === 'object') {
      const row = item as Record<string, unknown>;
      if ('group' in row) return [{ group: scalar(row.group), items: fromData(Array.isArray(row.items) ? row.items : [], disabled || !!row.disabled).flatMap(entry => 'group' in entry ? entry.items : [entry]) }];
      return [{ ...row, value: scalar(row.value), label: scalar(row.label ?? row.value), disabled: disabled || !!row.disabled } as Choice];
    }
    return [{ value: scalar(item), label: scalar(item), disabled }];
  });
  const rows = children != null ? fromChildren(children) : fromData([...(data || [])]);
  // Native select values identify the first matching option. Mantine requires
  // unique values, so keep that identity instead of crashing on duplicate data.
  const seen = new Set<string>();
  const unique = (item: Choice) => { if (seen.has(item.value)) return false; seen.add(item.value); return true; };
  return rows.flatMap<Choice | ChoiceGroup>(item => 'group' in item ? [{ ...item, items: item.items.filter(unique) }] : unique(item) ? [item] : []);
}

/** Existing callers read only target/currentTarget.value. This is a value
 * event adapter, not a fabricated browser select or a full SyntheticEvent. */
export function selectValueEvent(value: string, name?: string, id?: string): ChangeEvent<HTMLSelectElement> {
  const target = { value, name: name || '', id: id || '', type: 'select-one' };
  return { target, currentTarget: target, type: 'change' } as unknown as ChangeEvent<HTMLSelectElement>;
}

export const selectDropdownProps: NonNullable<SelectProps['comboboxProps']> = {
  withinPortal: true, floatingStrategy: 'fixed', position: 'bottom-start', width: 'target', zIndex: 400,
  preventPositionChangeWhenVisible: false,
  middlewares: {
    flip: { padding: 8 }, shift: { padding: 8 },
    size: { padding: 8, apply({ availableWidth, availableHeight, elements }) {
      elements.floating.style.maxWidth = `${Math.max(0, availableWidth)}px`;
      elements.floating.style.maxHeight = `${Math.max(0, availableHeight)}px`;
      elements.floating.style.setProperty('--rd-select-available-height', `${Math.max(0, availableHeight - 8)}px`);
    } },
  },
};

/** Stock Mantine Select with the application's existing native value contract.
 * The forwarded ref is its visible HTMLInputElement (measurement/focus safe). */
export const SelectField = forwardRef<HTMLInputElement, SelectFieldProps>(function SelectField({ children, data, value, defaultValue, onChange, onKeyDown, onInvalid, name, form, id, disabled, readOnly, required, multiple: _multiple, attributes, ...rest }, forwardedRef) {
  const options = useMemo(() => normalizeSelectData({ children, data }), [children, data]);
  const flat = options.flatMap(item => 'group' in item ? item.items : [item]);
  const first = flat.find(item => item.selected) || flat.find(item => !item.disabled);
  const controlled = value !== undefined;
  const [localValue, setLocalValue] = useState<string | null>(() => defaultValue !== undefined ? scalar(defaultValue) : first?.value ?? null);
  const selected = controlled ? scalar(value) : localValue;
  const [invalid, setInvalid] = useState(false);
  const input = useRef<HTMLInputElement>(null), opened = useRef(false);
  useImperativeHandle(forwardedRef, () => input.current!);
  useEffect(() => { if (!controlled && localValue == null && first) setLocalValue(first.value); }, [controlled, localValue, first?.value]);
  useEffect(() => { if (selected) setInvalid(false); }, [selected]);
  const props = rest as unknown as SelectProps;
  return <>
    <Select {...props} ref={input} id={id} name={name} form={form} disabled={disabled} readOnly={readOnly} required={required}
      value={selected} data={options} error={props.error || (invalid ? 'Choose an option.' : undefined)} allowDeselect={false} clearable={false}
      comboboxProps={selectDropdownProps} maxDropdownHeight="min(18rem, var(--rd-select-available-height, calc(100dvh - 32px)))"
      scrollAreaProps={{ scrollbars: 'y', overscrollBehavior: 'contain' }}
      attributes={{ ...attributes, options: { 'data-dialog-selector': '' } }}
      onDropdownOpen={() => { opened.current = true; }} onDropdownClose={() => { opened.current = false; }}
      onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
        if (event.key === 'Escape' && (opened.current || event.currentTarget.getAttribute('aria-expanded') === 'true')) { event.preventDefault(); event.stopPropagation(); }
        (onKeyDown as unknown as ((event: KeyboardEvent<HTMLInputElement>) => void) | undefined)?.(event);
      }}
      onChange={(next, option) => {
        if (disabled || readOnly || !option || option.disabled) return;
        const nextValue = scalar(next);
        // Match native select semantics: reselecting the displayed value must
        // not repeat a settings write. A failed controlled write still leaves
        // the previous displayed value, so choosing the new value can retry.
        if (nextValue === selected) return;
        if (!controlled) setLocalValue(nextValue); setInvalid(!!required && !nextValue);
        onChange?.(selectValueEvent(nextValue, name, id));
      }}
      renderOption={({ option, checked }) => { const item = option as Choice; return <Group gap="xs" wrap="nowrap">{checked && <CheckIcon size={12} aria-hidden="true" />}<span title={item.title} aria-label={item.ariaLabel} style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{item.label}</span></Group>; }} />
    {/* Mantine's hidden named input supplies FormData. A readonly combobox is
        excluded from native required validation, so an unnamed offscreen text
        input preserves the existing required gate and focuses the real field. */}
    {required && !readOnly && <input type="text" form={form} value={selected || ''} onChange={() => {}} required disabled={disabled} tabIndex={-1} aria-hidden="true" data-select-required-proxy=""
      style={{ position: 'fixed', width: 1, height: 1, opacity: 0, pointerEvents: 'none', left: 0, top: 0, padding: 0, border: 0 }}
      onInvalid={event => { event.preventDefault(); setInvalid(true); input.current?.focus(); onInvalid?.(event as unknown as React.FormEvent<HTMLSelectElement>); }} />}
  </>;
});
