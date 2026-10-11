export function slugify(title: string): string {
  return title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-");
}

export function parseQuantity(text?: string | null): number | undefined {
  if (text == null || text.trim() === "") {
    return undefined;
  }
  const n = Number.parseInt(text, 10);
  return Number.isNaN(n) ? undefined : n;
}

export function pad(value: string | number, width = 8): string {
  return String(value).padStart(width, " ");
}
