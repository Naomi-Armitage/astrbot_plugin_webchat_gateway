/** Stable Drop timeline order; ids only break ties at the same second. */
export interface DropOrderKey {
  created_at: number;
  id: number;
}

export function compareDropNewestFirst(a: DropOrderKey, b: DropOrderKey): number {
  return b.created_at - a.created_at || b.id - a.id;
}

export function isOlderDropMessage(item: DropOrderKey, cursor: DropOrderKey): boolean {
  return compareDropNewestFirst(item, cursor) > 0;
}
