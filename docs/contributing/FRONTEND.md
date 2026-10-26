# Frontend contracts

[Development](README.md) · [Testing](TESTING.md)

The UI is bundled plain JavaScript/CSS with no build step or external assets. `app.js` owns dashboard navigation, polling and configuration editors; `latency-chart.js`, `drag-order.js`, `select-control.js`, `speed-browser.js` and `speed-ui.js` own their respective components. Keep new served assets in the server allowlist and package data.

## Rendering and state

- Build untrusted names/diagnostics as text, not HTML. Use relative API/asset URLs so HTTPS path prefixes work.
- Dashboard polling does not overlap requests. Validate selection, resource, revision and generation before rendering an asynchronous response. Preserve drafts, expanded diagnostics, focused charts and loaded raw results across refreshes.
- Custom ranges store a rolling duration, not fixed local timestamps: 1 second–14 days, positive decimal hours/days, with one day = 24 elapsed hours. End at server time; retain valid selection after invalid input. Session storage binds the choice to the current CSRF session.
- Logout clears private UI state. Browser connectivity/API errors must not become monitoring observations.

## Components

The Swiss After Dark layout uses charcoal/bone/scarlet colors, labeled rail icons, title-first resource tiles and an independent upper-right Edit button. State changes retain order and geometry. Whole-tile navigation uses a real link; Edit stays above its hit region. Hover lightens the tile without moving it; keyboard focus remains visible.

Availability strips stay 12 px high and full inner width, including reorder mode. Start/mid/end labels describe the whole requested window; long ranges include dates and accessible full timestamps/offsets. Overview strips navigate; detailed charts support pointer/touch/keyboard inspection. Tooltips show mean, samples, extrema and only material counts/precision notes; missing values remain gaps. Do not paint unknown time as known for screenshots.

Reordering preserves field nodes and drafts, supports pointer capture, edge scroll, keyboard pickup/move/drop and Escape rollback. Compact check rows use six-dot grips; Done retains a draft, Save persists it. FLIP moves last 180 ms unless reduced motion is requested. Cancel animations on disable/destroy or preference changes. Reveal invalid collapsed fields before focusing them.

Styled selects retain native values/validation. Menus use the Popover API top layer; unsupported browsers retain native selects. Observe inserted controls/options and disabled state; call `SelectControl.sync(select)` after programmatic value changes. Preserve labels, immutable check types, type-ahead, selection, Escape-within-dialog, Tab dismissal and viewport bounds. Test visible controls, not hidden-native shortcuts.

## Speed UI lifecycle

Keep session/run epoch separate from poll epoch. Suspend and invalidate polling on `beforeunload`/`pagehide`; resume on `pageshow` or the next rendered frame when navigation does not proceed. Timers alone are not a reliable outgoing-document boundary in WebKit. Guard late detailed-result assignments, not just the initial state request.

Cancelled navigation must not discard browser-run callbacks, and page departure must not cancel independent node runs. Hidden-tab behavior remains distinct from navigation. API failures stay visible and retry through normal polling; do not suppress page errors or weaken security to hide races.
