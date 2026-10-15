/** Reads a quantity typed by a shopper: "3" is 3, blank input means "no quantity". */
export function parseQuantity(text: string | null): number | undefined {
  if (text === null || text.trim() === "") {
    return undefined;
  }
  const n = Number.parseInt(text, 10);
  return Number.isNaN(n) ? undefined : n;
}
