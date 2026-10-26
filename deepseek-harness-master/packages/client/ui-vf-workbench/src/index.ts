/**
 * VideoForge workbench, host half. Pure UI plugin: the empty apply exists so
 * the package appears in the host cordis list; the browser half ships via
 * exports["./client"], discovered through package.json `dsh.client`.
 */

/** Host plugin body — no host-side behavior for this surface plugin. */
export function apply(): void {}
