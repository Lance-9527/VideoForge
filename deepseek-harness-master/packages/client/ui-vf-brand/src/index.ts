/**
 * VideoForge shell, host half. The browser half ships through `exports["./client"]`,
 * so the Loader still gets a host-side row for this package.
 */

/** Host plugin body — this package contributes browser presentation only. */
export function apply(): void {}
