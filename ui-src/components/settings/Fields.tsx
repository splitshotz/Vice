import {useId, useState, type ReactNode} from 'react';
import {api} from '../../lib/api';
import {t} from '../../lib/i18n';

/**
 * One setting: what it is on the left, the control on the right.
 *
 * `stack` puts the control underneath instead, for the ones that need the
 * whole width (track builder, textareas, the sound grid).
 */
/**
 * A note's tone is what makes it readable at a glance: `plain` reads as help
 * text, so a note that is only restating what the setting does does not shout
 * for attention the way an actual warning has to.
 */
export interface RowNote {
  text: string;
  tone?: 'plain' | 'accent' | 'warning';
}

export function Row({
  label,
  help,
  note,
  stack,
  children,
}: {
  label: string;
  help?: ReactNode;
  note?: RowNote | null;
  stack?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="srow" data-stack={stack || undefined}>
      <div className="srow-label">
        <strong>{label}</strong>
        {help ? <span>{help}</span> : null}
        {note?.text ? (
          <span className="srow-note" data-tone={note.tone ?? 'plain'}>
            {note.text}
          </span>
        ) : null}
      </div>
      <div className="srow-control">{children}</div>
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      className="switch"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}>
      <span className="switch-thumb" />
    </button>
  );
}

export function Select({
  value,
  onChange,
  options,
  disabled,
  label,
}: {
  value: string;
  onChange: (next: string) => void;
  /** Flat options, or groups of them. */
  options: Array<[string, string]> | Array<{group: string; options: Array<[string, string]>}>;
  disabled?: boolean;
  label: string;
}) {
  const grouped = options.length > 0 && !Array.isArray(options[0]);
  return (
    <div className="select-wrap">
      <select
        className="select"
        value={value}
        aria-label={label}
        disabled={disabled}
        onChange={e => onChange(e.target.value)}>
        {grouped
          ? (options as Array<{group: string; options: Array<[string, string]>}>).map(g => (
              <optgroup key={g.group} label={g.group}>
                {g.options.map(([v, text]) => (
                  <option key={v} value={v}>
                    {text}
                  </option>
                ))}
              </optgroup>
            ))
          : (options as Array<[string, string]>).map(([v, text]) => (
              <option key={v} value={v}>
                {text}
              </option>
            ))}
      </select>
      <Chevron />
    </div>
  );
}

export function Slider({
  value,
  min,
  max,
  step,
  onChange,
  format,
  label,
  exact,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (next: number) => void;
  format: (value: number) => string;
  label: string;
  /** Lets the readout be clicked to type a value in whole units, for ranges
   * too long to land on an exact number by dragging: clip length steps 5 s
   * across half an hour, so 60 s sat between two pixels (#240). */
  exact?: boolean;
}) {
  const [typing, setTyping] = useState<string | null>(null);
  // The filled portion is painted from a custom property so the track can be
  // one element rather than a stack of them.
  const filled = ((value - min) / (max - min)) * 100;
  const commit = () => {
    const next = Math.round(Number(typing));
    if (typing !== null && typing.trim() !== '' && Number.isFinite(next)) {
      onChange(Math.min(max, Math.max(min, next)));
    }
    setTyping(null);
  };
  return (
    <div className="slider">
      <input
        type="range"
        value={value}
        min={min}
        max={max}
        step={step}
        aria-label={label}
        style={{['--filled' as string]: `${Math.min(100, Math.max(0, filled))}%`}}
        onChange={e => onChange(Number(e.target.value))}
      />
      {!exact ? (
        <span className="slider-value mono">{format(value)}</span>
      ) : typing === null ? (
        <button
          type="button"
          className="slider-value slider-value-button mono"
          aria-label={t('settings.typeExact', {label})}
          title={t('settings.typeExact', {label})}
          onClick={() => setTyping(String(value))}>
          {format(value)}
        </button>
      ) : (
        <input
          type="number"
          className="slider-value slider-exact mono"
          aria-label={t('settings.typeExact', {label})}
          value={typing}
          min={min}
          max={max}
          step={1}
          autoFocus
          onFocus={e => e.currentTarget.select()}
          onChange={e => setTyping(e.target.value)}
          onBlur={commit}
          onKeyDown={e => {
            if (e.key === 'Enter') commit();
            if (e.key === 'Escape') {
              e.stopPropagation();
              setTyping(null);
            }
          }}
        />
      )}
    </div>
  );
}

export function TextField({
  value,
  onChange,
  placeholder,
  label,
  mono,
  wide,
  type = 'text',
  min,
  max,
}: {
  value: string | number;
  onChange: (next: string) => void;
  placeholder?: string;
  label: string;
  mono?: boolean;
  wide?: boolean;
  type?: 'text' | 'number' | 'password';
  min?: number;
  max?: number;
}) {
  return (
    <input
      autoComplete={type === 'password' ? 'off' : undefined}
      className="text-input"
      data-mono={mono || undefined}
      data-wide={wide || undefined}
      type={type}
      min={min}
      max={max}
      value={value}
      placeholder={placeholder}
      aria-label={label}
      spellCheck={false}
      onChange={e => onChange(e.target.value)}
    />
  );
}

export function TextArea({
  value,
  onChange,
  placeholder,
  label,
  rows = 3,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  label: string;
  rows?: number;
}) {
  return (
    <textarea
      className="text-area mono"
      rows={rows}
      value={value}
      placeholder={placeholder}
      aria-label={label}
      spellCheck={false}
      onChange={e => onChange(e.target.value)}
    />
  );
}

/** The five custom notification sounds, as a labelled grid. */
export function SoundGrid({
  fields,
  values,
  onChange,
}: {
  fields: ReadonlyArray<readonly [string, string, string]>;
  values: Record<string, string>;
  onChange: (key: string, next: string) => void;
}) {
  const id = useId();
  // Why a field's file did not play, so nobody has to go reading a log (#297).
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [trying, setTrying] = useState('');
  const tryIt = (key: string) => {
    const path = (values[key] ?? '').trim();
    if (!path) return;
    setTrying(key);
    setProblems(p => ({...p, [key]: ''}));
    void api
      .trySound(path)
      .then(r => setProblems(p => ({...p, [key]: r.problem || ''})))
      .catch(() => setProblems(p => ({...p, [key]: 'unplayable'})))
      .finally(() => setTrying(k => (k === key ? '' : k)));
  };
  return (
    <div className="sound-grid">
      {fields.map(([key, labelKey, placeholder]) => {
        const problem = problems[key];
        const empty = !(values[key] ?? '').trim();
        return (
          <div key={key} className="sound-grid-field">
            <label htmlFor={`${id}-${key}`}>{t(labelKey)}</label>
            <div className="sound-grid-row">
              <input
                id={`${id}-${key}`}
                className="text-input"
                data-mono
                value={values[key] ?? ''}
                placeholder={placeholder}
                spellCheck={false}
                aria-invalid={problem ? true : undefined}
                onChange={e => {
                  setProblems(p => ({...p, [key]: ''}));
                  onChange(key, e.target.value);
                }}
              />
              <button
                type="button"
                className="btn btn-quiet btn-sm sound-grid-play"
                disabled={empty || trying === key}
                aria-label={t('settings.soundPlayAria', {name: t(labelKey)})}
                title={t('settings.soundPlayAria', {name: t(labelKey)})}
                onClick={() => tryIt(key)}>
                <span className="sound-pack-play" aria-hidden="true" />
              </button>
            </div>
            {problem ? (
              <span className="sound-grid-problem" role="status">
                {t(`settings.soundProblem.${problem}`)}
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

const Chevron = () => (
  <svg
    className="select-chevron"
    width={14}
    height={14}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true">
    <path d="m6 9 6 6 6-6" />
  </svg>
);
