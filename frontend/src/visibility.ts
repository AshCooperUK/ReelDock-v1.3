/**
 * Shared hidden-tab rule for polling loops (the NetworkActivity pattern):
 * a loop does not schedule its next request while the tab is hidden, and
 * `resume` runs once the tab becomes visible again.
 */
export function pageHidden() { return typeof document !== 'undefined' && document.hidden; }

export function onVisible(resume: () => void) {
 const handler = () => { if (!document.hidden) resume(); };
 document.addEventListener('visibilitychange', handler);
 return () => document.removeEventListener('visibilitychange', handler);
}
