import { useSyncExternalStore } from "react";
import { CircleHalfIcon } from "@phosphor-icons/react/dist/ssr/CircleHalf";
import { DesktopIcon } from "@phosphor-icons/react/dist/ssr/Desktop";
import { MoonIcon } from "@phosphor-icons/react/dist/ssr/Moon";
import { SunIcon } from "@phosphor-icons/react/dist/ssr/Sun";
import { IconButton } from "@langchain/macaw-components/IconButton";
import { useColorScheme, type ThemeMode } from "@langchain/macaw-components/hooks/useColorScheme";

const NEXT: Record<ThemeMode, ThemeMode> = { light: "dark", dark: "system", system: "light" };
const ICONS = { light: SunIcon, dark: MoonIcon, system: DesktopIcon };
const LABELS: Record<ThemeMode, string> = { light: "Light", dark: "Dark", system: "System" };

const subscribe = () => () => {};
const isClient = () => true;
const isServer = () => false;

/**
 * Cycles light → dark → system. `AppThemeProvider` reads `localStorage` in its
 * initial state, so the mode-dependent icon only renders after mount; until
 * then a stable placeholder matches the server markup.
 */
export function ThemeToggle() {
  const { mode, setMode } = useColorScheme();
  // false during SSR and hydration, true afterwards.
  const mounted = useSyncExternalStore(subscribe, isClient, isServer);

  const label = mounted ? `Theme: ${LABELS[mode]}. Switch to ${LABELS[NEXT[mode]].toLowerCase()}` : "Switch theme";
  return (
    <IconButton
      icon={mounted ? ICONS[mode] : CircleHalfIcon}
      iconWeight="regular"
      label={label}
      variant="plain"
      color="secondary"
      size="sm"
      data-theme-toggle={mounted ? mode : undefined}
      onClick={() => setMode(NEXT[mode])}
    />
  );
}
