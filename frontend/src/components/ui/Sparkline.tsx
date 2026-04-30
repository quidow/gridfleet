import { useMemo } from 'react';
import type { SVGProps } from 'react';

const FILL_OPACITY = 0.15;

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  className?: string;
  fillClassName?: string;
  ariaLabel?: string;
}

function buildStrokePath(values: number[], width: number, height: number): string | null {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min;
  if (range === 0) {
    const flatY = height / 2;
    return `M 0 ${flatY} H ${width}`;
  }
  const stepX = width / (values.length - 1);
  return values
    .map((value, index) => {
      const x = index * stepX;
      const y = height - ((value - min) / range) * height;
      const cmd = index === 0 ? 'M' : 'L';
      return `${cmd} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(' ');
}

function buildFillPath(strokePath: string, width: number, height: number): string {
  return `${strokePath} L ${width.toFixed(2)} ${height.toFixed(2)} L 0 ${height.toFixed(2)} Z`;
}

export default function Sparkline({
  values,
  width = 64,
  height = 28,
  className,
  fillClassName,
  ariaLabel,
}: SparklineProps) {
  const effectiveFillClassName = fillClassName ?? className;
  const strokePath = useMemo(() => buildStrokePath(values, width, height), [values, width, height]);
  const fillPath = useMemo(
    () => (strokePath && effectiveFillClassName ? buildFillPath(strokePath, width, height) : null),
    [strokePath, effectiveFillClassName, width, height],
  );
  if (!strokePath) return null;

  const svgProps: SVGProps<SVGSVGElement> = {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    className,
    role: ariaLabel ? 'img' : 'presentation',
  };
  if (ariaLabel) svgProps['aria-label'] = ariaLabel;

  return (
    <svg {...svgProps} preserveAspectRatio="none">
      {fillPath ? (
        <path d={fillPath} fill="currentColor" fillOpacity={FILL_OPACITY} className={effectiveFillClassName} vectorEffect="non-scaling-stroke" />
      ) : null}
      <path
        d={strokePath}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
