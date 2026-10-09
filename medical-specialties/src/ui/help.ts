export function setupHelp(): { toggle: () => void; isOpen: () => boolean } {
  const overlay = document.getElementById('help')!;
  const open = document.getElementById('help-btn')!;
  const close = document.getElementById('help-close')!;
  const set = (v: boolean) => overlay.toggleAttribute('hidden', !v);
  open.addEventListener('click', () => set(true));
  close.addEventListener('click', () => set(false));
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) set(false);
  });
  return { toggle: () => set(overlay.hasAttribute('hidden')), isOpen: () => !overlay.hasAttribute('hidden') };
}
