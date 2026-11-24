/** The icon set the shell needs. Stroked, 24px grid, sized by the caller. */

type IconProps = {size?: number; className?: string};

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
});

export const IconHome = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V21h14V9.5" />
  </svg>
);

export const IconClips = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <rect x="3" y="5" width="18" height="14" rx="3" />
    <path d="M10 9.5v5l4-2.5z" />
  </svg>
);

export const IconImages = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <rect x="3" y="4" width="18" height="16" rx="3" />
    <circle cx="8.5" cy="9.5" r="1.6" />
    <path d="m4 17 4.5-4.5 3.5 3.5 3-2.5L20 18" />
  </svg>
);

export const IconEditor = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <circle cx="6" cy="7" r="3" />
    <circle cx="6" cy="17" r="3" />
    <path d="M20 5 9 15M20 19 9 9" />
  </svg>
);

export const IconSettings = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M4 7h8M16 7h4M4 12h4M12 12h8M4 17h10M18 17h2" />
    <circle cx="14" cy="7" r="2" />
    <circle cx="8" cy="12" r="2" />
    <circle cx="16" cy="17" r="2" />
  </svg>
);

export const IconAbout = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </svg>
);

export const IconSearch = ({size = 16, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

export const IconWarning = ({size = 18, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 8v4.5M12 16h.01" />
  </svg>
);

export const IconClose = ({size = 15, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M18 6 6 18M6 6l12 12" />
  </svg>
);

export const IconMinimize = ({size = 15, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M5 12h14" />
  </svg>
);

export const IconPower = ({size = 15, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M12 4v8" />
    <path d="M7.5 7a7 7 0 1 0 9 0" />
  </svg>
);

export const IconPlaylist = ({size = 16, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M4 7h11M4 12h11M4 17h7" />
    <path d="M18 11v7" />
    <circle cx="16.5" cy="18" r="1.6" />
  </svg>
);

/** The brand mark: the recording ring Vice has always used. */
export const IconMark = ({size = 20, className}: IconProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.25}
    className={className}
    aria-hidden="true">
    <circle cx="12" cy="12" r="9.5" />
    <circle cx="12" cy="12" r="3" fill="currentColor" />
  </svg>
);

export const IconCheck = ({size = 14, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="m4 12.5 5 5L20 6.5" />
  </svg>
);

export const IconHelp = ({size = 14, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <circle cx="12" cy="12" r="9.5" />
    <path d="M9.4 9.2a2.7 2.7 0 0 1 5.2.9c0 1.8-2.6 2.7-2.6 2.7" />
    <path d="M12 17h.01" />
  </svg>
);

export const IconSparkle = ({size = 14, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z" />
    <path d="M18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" />
  </svg>
);

export const IconDownload = ({size = 12, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="m7 10 5 5 5-5" />
    <path d="M12 15V3" />
  </svg>
);

export const IconMore = ({size = 16, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <circle cx="12" cy="5" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none" />
  </svg>
);

export const IconPlus = ({size = 14, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M12 5v14M5 12h14" />
  </svg>
);

/* Corners pointing out to grow, pointing in to shrink. The same pair the
   browser chrome uses, which is what people already read as this gesture. */
export const IconExpand = ({collapse, size = 15}: IconProps & {collapse?: boolean}) => (
  <svg {...base(size)} aria-hidden="true">
    <path
      d={
        collapse
          ? 'M9 3v4a2 2 0 0 1-2 2H3M15 3v4a2 2 0 0 0 2 2h4M9 21v-4a2 2 0 0 0-2-2H3M15 21v-4a2 2 0 0 1 2-2h4'
          : 'M3 9V5a2 2 0 0 1 2-2h4M21 9V5a2 2 0 0 0-2-2h-4M3 15v4a2 2 0 0 0 2 2h4M21 15v4a2 2 0 0 1-2 2h-4'
      }
    />
  </svg>
);

/** On a media server. `filled` for a clip that lives only there. */
export const IconCloud = ({size = 14, className, filled}: IconProps & {filled?: boolean}) => (
  <svg {...base(size)} fill={filled ? 'currentColor' : 'none'} className={className} aria-hidden="true">
    <path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9a4.5 4.5 0 0 1-.5 9z" />
  </svg>
);

export const IconUpload = ({size = 14, className}: IconProps) => (
  <svg {...base(size)} className={className} aria-hidden="true">
    <path d="M12 16V4M7 9l5-5 5 5M5 20h14" />
  </svg>
);
