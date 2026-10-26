/**
 * Dispatch the current notice without interpreting its business payload.
 * @param props - Current notice, dismissal, and the declared keyed renderer.
 * @returns The selected notice presentation, or nothing.
 */
export function RowActionToast({ useToast, dismissToast, renderSlot }) {
    const notice = useToast(current => current);
    if (notice === null)
        return null;
    return renderSlot('workspace.toast', { ...notice, dismissToast }, { entryKey: notice.kind });
}
//# sourceMappingURL=RowActionToast.js.map