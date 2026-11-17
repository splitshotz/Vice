import {useEffect, useState} from 'react';

import {MOD_CODES, codeToEvdev, comboFromEvent} from '../../lib/hotkeyCapture';
import {comboLabel} from '../../lib/format';
import {t} from '../../lib/i18n';

/**
 * Click, then press the combo you want. A lone modifier only arms it, so
 * Alt+F9 is reachable; Escape leaves the binding alone.
 */
export function KeyCapture({
  value,
  onCapture,
  onUnsupported,
  compact,
}: {
  value: string;
  onCapture: (evdev: string) => void;
  onUnsupported: () => void;
  compact?: boolean;
}) {
  const [listening, setListening] = useState(false);

  useEffect(() => {
    if (!listening) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (MOD_CODES.has(e.code)) return;
      if (e.code === 'Escape') {
        setListening(false);
        return;
      }
      const main = codeToEvdev(e.code);
      if (!main) {
        onUnsupported();
        return;
      }
      setListening(false);
      onCapture(comboFromEvent(e, main));
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [listening, onCapture, onUnsupported]);

  return (
    <button
      type="button"
      className="key-capture mono"
      data-listening={listening || undefined}
      data-compact={compact || undefined}
      onClick={() => setListening(true)}>
      {listening ? t('keyCapture.pressKey') : value ? comboLabel(value) : t('keyCapture.setKey')}
    </button>
  );
}
