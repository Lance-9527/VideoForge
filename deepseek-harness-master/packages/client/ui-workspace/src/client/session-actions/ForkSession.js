import { jsx as _jsx } from "react/jsx-runtime";
/** The fork action: one `sidebar.workspaces.session.menu.item` row. */
import { IconBranchOutlineRegular, MenuItemButton } from '@deepseek-ai/dsh-client-ui-primitives';
/**
 * Menu row (order 300): fork at the Session's last completed turn; the child
 * arrives through the Host list beside its source. Absent when the access
 * snapshot excludes `session/fork` for this Session.
 * @param props - owner share, menu open state, access Hook, and the fork share.
 * @returns the row, or nothing when the caller may not fork this Session.
 */
export function ForkSessionMenuItem({ sessionId, useMenuOpenState, useShortcuts, useSessionAccess, forkSession, t, }) {
    const [, setMenuOpen] = useMenuOpenState();
    const shortcut = useShortcuts(rows => rows.find(row => row.id === 'session.fork'));
    const forkAllowed = useSessionAccess(sessionId, 'session/fork');
    if (!forkAllowed)
        return null;
    return (_jsx(MenuItemButton, { shortcut: shortcut, icon: _jsx(IconBranchOutlineRegular, {}), onSelect: () => {
            setMenuOpen(false);
            forkSession(sessionId);
        }, children: t('menu.fork') }));
}
//# sourceMappingURL=ForkSession.js.map