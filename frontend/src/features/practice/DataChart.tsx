import { Alert, Typography } from "antd";
import { useId } from "react";
import type { LearningChart } from "../../api/learningClient";
import "./content.css";

type Review = "consistent" | "needs_confirmation" | "conflicting";

/** SVG generated only from the finite DTO; all model strings remain text. */
export function DataChart({ chart, review = "needs_confirmation" }: { chart: LearningChart; review?: Review }) {
  const id = useId();
  const left = 76, top = 26, width = 520, height = 230;
  const xs = chart.points.map(p => p.x), ys = chart.points.map(p => p.y);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymin = Math.min(...ys, 0), ymax = Math.max(...ys, 0);
  // Normalize before subtraction to keep even very large finite values finite.
  const ratio = (value: number, min: number, max: number) => {
    if (min === max) return 0.5;
    const scale = Math.max(Math.abs(min), Math.abs(max), 1);
    return (value / scale - min / scale) / (max / scale - min / scale);
  };
  const x = (value: number) => left + 12 + ratio(value, xmin, xmax) * (width - 24);
  const y = (value: number) => top + height - ratio(value, ymin, ymax) * height;
  const xLabel = `${chart.x_axis.label} (${chart.x_axis.unit})`;
  const yLabel = `${chart.y_axis.label} (${chart.y_axis.unit})`;
  const barWidth = xs.length === 1 ? 48 : Math.max(1, Math.min(...xs.slice(1).map((v, i) => x(v) - x(xs[i]))) * 0.6);
  const points = chart.points.map(p => `${x(p.x)},${y(p.y)}`).join(" ");
  const ticks = Array.from({ length: 5 }, (_, i) => ({ x: xmin * (1 - i / 4) + xmax * i / 4, y: ymin * (1 - i / 4) + ymax * i / 4 }));
  const format = (v: number) => Number(v.toPrecision(4)).toString();
  return <figure className="learning-chart">
    {review !== "consistent" && <Alert type="warning" title={review === "conflicting" ? "数据与答案依据冲突，暂缓使用" : "图表依据待确认，暂缓使用"} />}
    <Typography.Title level={5}>{chart.title}</Typography.Title>
    <svg viewBox="0 0 680 320" role="img" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}>
      <title id={`${id}-title`}>{chart.title}</title><desc id={`${id}-description`}>{xLabel}；{yLabel}；{chart.basis}</desc>
      <path d={`M${left},${top}V${top + height}H${left + width}`} stroke="#555" fill="none" />
      {ticks.map((t, i) => <g key={i} fill="#444" fontSize="11">
        <text x={x(t.x)} y={top + height + 18} textAnchor="middle">{format(t.x)}</text>
        <text x={left - 8} y={y(t.y) + 4} textAnchor="end">{format(t.y)}</text>
      </g>)}
      <text x={left + width / 2} y="305" textAnchor="middle">{xLabel}</text>
      <text transform="translate(18,140) rotate(-90)" textAnchor="middle">{yLabel}</text>
      {chart.kind === "line" ? <>
        <polyline points={points} stroke="#1677ff" strokeWidth="2" fill="none" />
        {chart.points.map((p, i) => <circle key={i} cx={x(p.x)} cy={y(p.y)} r="4" fill="#1677ff" />)}
      </> : chart.points.map((p, i) => <rect key={i} data-value={p.y} x={x(p.x) - barWidth / 2} y={Math.min(y(p.y), y(0))} width={barWidth} height={Math.abs(y(p.y) - y(0))} fill="#1677ff" />)}
    </svg>
    <figcaption>依据：{chart.basis}</figcaption>
    <div className="question-table-scroll"><table aria-label="图表数据"><thead><tr><th>{xLabel}</th><th>{yLabel}</th></tr></thead><tbody>{chart.points.map((point, i) => <tr key={i}><td>{point.x}</td><td>{point.y}</td></tr>)}</tbody></table></div>
  </figure>;
}
