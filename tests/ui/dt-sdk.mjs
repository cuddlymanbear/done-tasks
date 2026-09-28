/**
 * @hermes/plugin-sdk stub for the harness (ESM).
 *
 * The harness primes `globalThis.__DT_SDK__` with React-backed components
 * before importing the plugin; this module re-exports them by name, exactly the
 * way the desktop app's shim re-exports the live SDK namespace.
 */
const S = globalThis.__DT_SDK__

export default S
export const {
  Badge, Button, Codicon, EmptyState, ErrorState, ListRow, ROUTES_AREA, ScrollArea,
  SegmentedControl, SIDEBAR_NAV_AREA, Skeleton, Switch, Tip, cn, host, useQuery, useValue
} = S
