/* The map engine's naive numbers and stand-in choices: every one in this table, nowhere else in the engine.
   Plan: agent-ide/map-engine-plan.md (draft 4), sections 2.4, 2.5, 3.6, 7 and 8. Each entry says where it came from:
   (design) a number set as a design decision; (plan) the planner's stand-in, still open; (builder) chosen while building.
   All of them are naive: simplified rules, guesses to be replaced by measuring. */
const ENG_NAIVE = {
  // ---- urgency: what is lost per minute you have not noticed (plan 2.4), as a rate 0..1 (1 = a whole minute of the agent and
  // whatever waits on it, per minute). Bands: high >= .8, medium >= .3, low > 0.
  urgency: {
    ask: 1.0,              // (plan D3) only a request is the highest
    askChildParentStopped: 1.0,   // (plan 2.4) a child's request: high; "higher when its parent is waiting on it" is not knowable here, kept equal
    failed: 0.9,           // (plan A6) a failure sits just under a request
    failedChildParentRuns: 0.2,   // (plan D4) low while its parent runs and can deal with it
    finished: 0.5,         // (plan D3) a finished turn is medium
    finishedChild: 0.1,    // (plan D4, A8) low: the parent takes the result
    stalled: 0.5,          // (plan 2.4) medium, uncertain
    ctxNear: 0.1,          // (plan 2.4) context near its limit: low
  },
  bands: { high: 0.8, medium: 0.3 },
  // ---- the five levels (plan 3.6), lowest to highest; what urgency raises a label to (builder: medium counts as strong)
  levels: ['ground', 'quiet', 'plain', 'strong', 'call'],
  levelOfBand: { high: 'strong', medium: 'strong', low: 'plain', none: null },
  callMax: 4,              // (plan D5, section 7) the top level is scarce: about four at once; the rest of the asking take 'strong'
  // ---- thresholds read from the page's own rules (plan A11: unexamined); kept here so they can be seen in one place
  stalledAfter: 600,       // (page: STALL) running and silent ten minutes
  ctxStandsOut: 0.9,       // (plan section 7) the context figure stands out from about 90% of the compaction point
  compactNoteUntil: 0.15,  // (design) the compaction note shows until 15% of the window is used again
  // ---- the compaction signal (2026-11-22: part of the engine, with its own parameters, chosen like every other piece, and
  // given a low weight): one detail piece of the label,
  // 'compaction', saying the first line of the latest compaction's summary in plain words ("compacted: …"; "context compacted" when the
  // log has no summary). It is the last piece of every condition (below the request, the failure, what it does or said, the plan's
  // step, what it changed), so any other piece takes the room first. It is eligible from the compaction until compactNoteUntil of
  // the window is used again (the design rule above), in every condition but archived.
  compaction: {
    interest: 0.15,        // (builder, naive) its weight in the sheet and the surfaces: under every piece but a quiet agent's (0.1)
    minForm: 6,            // (builder, naive) said only in the fullest form (ENG_FORM 6): by load that is one to five agents in view
                           // (labelsByLoad), past five only zoomed near (labelsOpenByBand.near) or with the pointer on the label
                           // (ENG_ROW.hoverForm); the forms below it never carry it
    maxChars: 90,          // (builder, naive) the summary's line is cut on a whole word to this many letters before the label's width cuts it
  },
  // ---- standing (plan A19, three parts): each part is the agent's rank among the agents on the floor (0 lowest, 1 highest);
  // standing is their plain mean (builder: the simplest defensible combination). Recency is the order of last action, never age.
  // Total lines changed: the server now sends each write's lines touched (added plus removed, from the patch or edit it already
  // holds); made-up floors make them up. A whole-file write counts its lines as added.
  standingParts: { recency: 1, context: 1, lines: 1 },
  standingMargin: 0.08,    // (builder) an agent takes another's label or pointer only when ahead by this much (plan 5.5)
  // ---- seen (plan 2.6, P7): a change stands out for this long while the page is in front, then counts as shown (section 7: tens of s)
  shownAfter: 20,
  // ---- the agent tree's cap (engine/tree.js; integration round 3, coordinator's ruling: "a cap ... two fifths of the window is a fair
  // naive start"): the tree's box covers at most this share of the window's area; rows past it fold into one line at its foot. As a
  // share of the area it is two fifths of the height at 800x500 (where the cap was proposed), while a wider and taller window lets
  // the tree run longer, so nothing folds at 1440x900 with 20 agents (the ruling's other condition; two fifths of the height alone
  // would fold there). Rows that need you are never folded: the cap gives way to them.
  treeMaxArea: 0.15,       // (planner, naive)
  // ---- how much a floor label says, by load (integration piece 4, planner's starts; every one naive, to be judged by the picture).
  // n is the number of agents with a stack in view (zooming in leaves fewer in view, so it never shows less). Forms (engine/forms.js):
  // 2 the bare name line, 3 the name line ending in a few words of what the agent is doing or what came of it, 4 one line under the
  // name, 5 two lines (the middle form), 6 the fullest (ENG_FORM.detailLines[6] lines). need: the form for a set of like agents that needs you (asking, a
  // failure or finish not yet seen, stalled: anything with urgency, or on the engine's list); rest: every other set; open: the most a
  // set may open to, past its start, where that pushes nobody up a tier of the row (the middle band of zoom opens to at least 4, the
  // near band to 6); words: px the name line's words take at most. Rows that do not fit fall back form by form as before.
  // quiet: quiet agents (idle; a failure or finish seen) are labelled at this load, far out too, but only in room nobody else needs:
  // when one has no place it goes to its own stack, then shortens, then gives way, before any other label loses a line, a figure or
  // a letter for it (piece 4, builder H: an idle agent at 5 and at 8 had no label, and at 5 its want of room had cut every other
  // label to one line). stackFirst: a working agent's label with no room on the row stands at its own stack before it is left out
  // (builder H: at 5 agents in 800x500 a running agent was left with its mark only)
  labelsByLoad: [
    { upTo: 5, need: 6, rest: 6, open: 6, words: 150, quiet: true, stackFirst: true },      // (planner, naive) one to five agents: every agent the fullest form
    { upTo: 12, need: 5, rest: 3, open: 3, words: 150, quiet: true },      // (planner, naive) six to twelve: who needs you two lines, the rest one line of words
    { upTo: Infinity, need: 3, rest: 3, open: 4, words: 100, inPlace: true },   // (coordinator's ruling, round 2) above twelve the words
                                                      // take only the room the figures took; nobody moves for them    // (planner, naive) above twelve: as before (the name line, one line under it where
                                                      // it pushes nobody up), the name line now carrying words
  ],
  labelsOpenByBand: { far: 0, middle: 5, near: 6 },   // (builder, naive) zooming in opens a set to at least this
  nameWordsW: 150,         // (builder, naive) px the name line's words take at most (words: the same by load; above twelve 100, as 150
                           // left four of twenty agents with no label); a running agent's words reserve all of it, so a new action
                           // replaces the words in place and the box keeps its width
  changeW: 560,            // (planner, naive) px the transcript panel widens to while an agent's change is open (engine/change.js): code
                           // lines wrap badly at the conversation's width; back to the conversation, it goes back
};
