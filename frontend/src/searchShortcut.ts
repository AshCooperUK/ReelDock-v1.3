// '/' moves focus to the search box on the current page, as on many web apps. No new control is added.
type KeyLike = { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean; defaultPrevented: boolean; target: EventTarget | null; preventDefault(): void };
type InputLike = { focus(): void; select(): void; getClientRects(): { length: number } };
type DocumentLike = { querySelector(selector: string): unknown; querySelectorAll(selector: string): ArrayLike<unknown> };

function editable(target: EventTarget | null) {
  const element = target as { tagName?: string; isContentEditable?: boolean } | null;
  return !!element && (['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName || '') || !!element.isContentEditable);
}

/** Returns true when the key press was used to focus a search box. */
export function focusSearch(event: KeyLike, doc: DocumentLike = document) {
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented || editable(event.target)) return false;
  if (doc.querySelector('[role="dialog"]')) return false; // Never reach behind an open window.
  const visible = (selector: string) => Array.from(doc.querySelectorAll(selector) as ArrayLike<InputLike>).find(item => item.getClientRects().length > 0);
  const input = visible('[data-global-search] input') || visible('[data-page-search] input') || visible('main input[type="search"]');
  if (!input) return false;
  event.preventDefault();
  input.focus();
  input.select();
  return true;
}
