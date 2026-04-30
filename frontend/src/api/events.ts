import api from './client';
import type { EventCatalogEntry } from '../types';

export async function fetchEventCatalog(): Promise<EventCatalogEntry[]> {
  const { data } = await api.get('/events/catalog');
  return data.events;
}
