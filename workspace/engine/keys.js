/* The page's keys that act from anywhere: one table, in one place (integration brief C, 2026-11-16). A later piece fills it with
   "stop", "go to by name" and the like; today it holds one entry. Nothing else on the page binds these keys.
   Each entry: name; what it does, in plain words (the tree's head and tips read it); the key, matched on the physical key (ev.code)
   so a keyboard layout does not move it; whether it also acts while the cursor is in a box; and what it runs.
   The rule for a key that acts while the cursor is in a box: it must never type a character there, so it carries Control (on a
   Mac Control with a letter or a mark types nothing; Option does, and Command keys belong to the browser). The page calls
   engKeyDown(ev) once, before anything else looks at the key (see the page's one keydown listener); a matched entry stops the
   key there: nothing else acts on it and nothing is typed.
   Origins: (brief) brief C; (builder) chosen while building. All naive. */
const ENG_KEYS = [
  // empty since 2026-11-22 (no page-wide keyboard shortcuts are needed yet). Until then it held 'next': Control +
  // full stop, "go to the next agent that needs you, most pressing first" (engNeedStep('key')); a click on the agent list's
  // "N needs you" line still does that
];
const ENGK = { last: null };          // drivers: the last key the table took (its entry and where the cursor was)

// does this key press match an entry (exact modifiers: Control alone, or as the entry says)
function engKeyMatch(k, ev) {
  return ev.code === k.code && !!ev.ctrlKey === !!k.ctrl && !!ev.altKey === !!k.alt && !!ev.shiftKey === !!k.shift && !ev.metaKey;
}
const engKeyInBox = t => !!t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
// the page's keydown listener calls this first; true: the table took the key (nothing else should act on it)
function engKeyDown(ev) {
  if (ev.repeat || ev.isComposing) return false;
  const inBox = engKeyInBox(ev.target);
  for (const k of ENG_KEYS) {
    if (!engKeyMatch(k, ev) || (inBox && !k.inBoxes)) continue;
    ev.preventDefault(); ev.stopPropagation();
    ENGK.last = { name: k.name, inBox, at: Date.now() };
    k.run(ev);
    return true;
  }
  return false;
}
// a key's name as the person reads it on this system (Mac: the Control sign; elsewhere: Ctrl+)
function engKeyLabel(name) {
  const k = ENG_KEYS.find(x => x.name === name); if (!k) return '';
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  const ch = { Period: '.', Comma: ',', Slash: '/' }[k.code] || k.code.replace(/^Key/, '');
  return mac ? (k.ctrl ? '⌃' : '') + (k.alt ? '⌥' : '') + (k.shift ? '⇧' : '') + ch : [k.ctrl && 'Ctrl', k.alt && 'Alt', k.shift && 'Shift', ch].filter(Boolean).join('+');
}
// drivers: the table and the last key it took
function engKeysState() { return { keys: ENG_KEYS.map(k => ({ name: k.name, what: k.what, label: engKeyLabel(k.name), inBoxes: k.inBoxes })), last: ENGK.last }; }
