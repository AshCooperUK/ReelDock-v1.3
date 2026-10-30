import type { DiscoverItem } from './Discover';

/** Catalogue release lists belong to search results, never download commands. */
export function acquisitionItem(item: DiscoverItem) {
 return {
  id:item.id, kind:item.kind, title:item.title.slice(0,300), externalId:item.externalId,
  year:item.year, provider:item.provider, image:item.image?.slice(0,400),
  description:item.description?.slice(0,800), genre:item.genre?.slice(0,80), rating:item.rating,
  videoId:item.videoId, channelId:item.channelId, channel:item.channel?.slice(0,180),
  published:item.published, releaseDate:item.releaseDate, backdrop:item.backdrop?.slice(0,400),
 };
}
