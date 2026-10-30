/** Display source dates without inventing a time for date-only metadata. */
export function mediaDate(value?: string | null, includeTime = true): string {
  if (!value) return '';
  if (!/^\d{4}-\d{2}-\d{2}(?:$|T)/.test(value)) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    if (parsed.toISOString().slice(0, 10) !== value) return '';
    return value.split('-').reverse().join('.');
  }
  const date = parsed.toLocaleDateString('en-GB', {
    day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'Europe/London',
  }).replaceAll('/', '.');
  const time = parsed.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Europe/London',
  });
  return includeTime ? `${date} ${time}` : date;
}

/** Accept UK entry, while preserving compatibility with pasted ISO dates. */
export function inputDate(value: string): string {
  const iso = /^\d{2}\.\d{2}\.\d{4}$/.test(value) ? value.split('.').reverse().join('-') : value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso) || !mediaDate(iso)) throw new Error('Use a valid date in DD.MM.YYYY format.');
  return iso;
}
