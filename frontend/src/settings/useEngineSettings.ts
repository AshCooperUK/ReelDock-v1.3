import { useRef } from 'react';
import { useAction, useSettings } from '../SettingsUI';

type Value = boolean | number | string;
type Payload<T> = { engine: string; values: T };

/** Shared save feedback and server-confirmed values for an engine's public preferences. */
export function useEngineSettings<T extends Record<string, Value>>(engine: string, csrf: string) {
  const url = `/api/settings/${encodeURIComponent(engine)}`;
  const resource = useSettings<Payload<T>>(url);
  const action = useAction(csrf);
  const saving = useRef(false);
  async function save(values: Partial<T>) {
    if (saving.current) return false;
    saving.current = true;
    try {
      const result = await action.run<Payload<T>>(url, { values });
      if (!result) return false;
      resource.setData(result);
      return true;
    } finally {
      saving.current = false;
    }
  }
  return { ...resource, ...action, values: resource.data?.values, loadError: resource.error, save };
}
