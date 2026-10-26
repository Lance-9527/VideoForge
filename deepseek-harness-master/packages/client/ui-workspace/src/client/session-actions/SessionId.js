import { jsx as _jsx } from "react/jsx-runtime";
/** Copy the row's Session ID without opening its history. */
import { IconCopyOutlineRegular, MenuItemButton } from '@deepseek-ai/dsh-client-ui-primitives';
/**
 * Copy the complete ID after dismissing the row menu.
 * @param props - Row identity, menu dismissal, clipboard action, and locale.
 * @returns The final built-in menu item.
 */
export function SessionIdMenuItem({ sessionId, useMenuOpenState, copySessionId, t }) {
    const [, setMenuOpen] = useMenuOpenState();
    return (_jsx(MenuItemButton, { icon: _jsx(IconCopyOutlineRegular, {}), onSelect: () => {
            setMenuOpen(false);
            void copySessionId(sessionId);
        }, children: t('menu.sessionId') }));
}
//# sourceMappingURL=SessionId.js.map