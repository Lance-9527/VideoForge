import { jsx as _jsx } from "react/jsx-runtime";
/** One presentation Factory and the adapters for keyed Workspace notices. */
import { IconWarningOutlineRegular, Toast } from '@deepseek-ai/dsh-client-ui-primitives';
/**
 * Render one notice using the shared Factory.
 * @param props - Keyed payload, injected display mapping, locale, and Factory renderer.
 * @returns The shared Toast occurrence.
 */
export function ToastEntry(props) {
    const { dismissToast, display, t, renderFactorySlot, seq } = props;
    return renderFactorySlot('workspace.toast.content', { ...display(props, t, dismissToast), seq, onDone: dismissToast });
}
/**
 * Keep the archive filter action reactive without subscribing unrelated notices.
 * @param props - Archive notice, display mapping, and the existing viewing store.
 * @returns The shared Toast occurrence with its current actions.
 */
export function ArchiveToastEntry(props) {
    const { dismissToast, display, filterAction, useStore, t, renderFactorySlot, seq } = props;
    const archivedVisible = useStore(state => (state.archivedFilter ?? 'default') !== 'default');
    const fields = display(props, t, dismissToast);
    return renderFactorySlot('workspace.toast.content', {
        ...fields,
        actions: [...fields.actions ?? [], ...archivedVisible ? [] : [filterAction(t, dismissToast)]],
        seq,
        onDone: dismissToast,
    });
}
/**
 * Present plain display data with the shared primitive's timing and portal.
 * @param props - Localized display fields, display identity, and dismissal.
 * @returns One Toast, remounted only for a new display identity.
 */
export function WorkspaceToast({ seq, text, tone, holdMs, actions, onDone }) {
    return _jsx(Toast, { text: text, onDone: onDone, ...tone === 'success' ? { tone: 'success' } : {}, ...tone === 'warning' ? { icon: _jsx(IconWarningOutlineRegular, {}) } : {}, ...holdMs === undefined ? {} : { holdMs }, ...actions === undefined ? {} : { actions } }, seq);
}
//# sourceMappingURL=Toast.js.map