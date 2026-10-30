/** Artwork sources already supported by the app's content security policy. */
export function downloadPicture(src?: string): string {
 if (!src) return '';
 if (/^(?:\/artwork\/[a-f0-9]{64}|\/api\/m3u\/[^/?]+\/artwork\?[^\s]*)$/.test(src)) return src;
 return /^https:\/\/i\.ytimg\.com\/vi(?:_webp)?\/[A-Za-z0-9_-]{11}\/[A-Za-z0-9_.-]+(?:\?[^\s#]*)?$/.test(src) ? src : '';
}
