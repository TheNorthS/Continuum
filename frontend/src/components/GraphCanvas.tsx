import { useEffect, useRef, useState } from 'react';
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from 'd3-force';
import type { Simulation, SimulationNodeDatum, SimulationLinkDatum } from 'd3-force';

export interface GraphNode {
  id: string;
  name: string;
  kind: string;
}
export interface GraphLink {
  id: string;
  source: string;
  target: string;
  predicate: string;
  evidence: string;
  filename: string;
  document_id: string;
  chunk_id: string;
  extraction: string;
}
export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}
interface Positioned extends GraphNode, SimulationNodeDatum {}
interface Edge extends SimulationLinkDatum<Positioned> {
  id: string;
  predicate: string;
}

interface Viewport {
  x: number;
  y: number;
  zoom: number;
}
interface Point {
  x: number;
  y: number;
}
interface Gesture {
  pointerId: number;
  node?: Positioned;
  startClient: Point;
  start: Point;
  viewport: Viewport;
  offset: Point;
  moved: boolean;
}
const INITIAL_VIEW: Viewport = { x: 0, y: 0, zoom: 1 };
const DRAG_THRESHOLD = 4;

export function GraphCanvas({
  data,
  selected,
  onSelect,
}: {
  data: GraphData;
  selected: string;
  onSelect: (id: string) => void;
}) {
  const [nodes, setNodes] = useState<Positioned[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [viewport, setViewport] = useState<Viewport>(INITIAL_VIEW);
  const [interacting, setInteracting] = useState(false);
  const view = useRef<Viewport>(INITIAL_VIEW);
  const svg = useRef<SVGSVGElement>(null);
  const live = useRef<Positioned[]>([]);
  const gesture = useRef<Gesture | null>(null);
  const simulationRef = useRef<Simulation<Positioned, Edge> | null>(null);

  function updateView(next: Viewport) {
    view.current = next;
    setViewport(next);
  }

  // SVG coordinates, including the margins created by preserveAspectRatio="xMidYMid meet".
  function pointAt(clientX: number, clientY: number): Point | null {
    const bounds = svg.current?.getBoundingClientRect();
    if (!bounds || !bounds.width || !bounds.height) return null;
    const scale = Math.min(bounds.width / 900, bounds.height / 480);
    return {
      x: (clientX - bounds.left - (bounds.width - 900 * scale) / 2) / scale,
      y: (clientY - bounds.top - (bounds.height - 480 * scale) / 2) / scale,
    };
  }

  function zoomAt(factor: number, anchor: Point = { x: 450, y: 240 }) {
    if (gesture.current) return;
    const current = view.current;
    const zoom = Math.max(0.3, Math.min(3, current.zoom * factor));
    const ratio = zoom / current.zoom;
    updateView({
      x: anchor.x - (anchor.x - current.x) * ratio,
      y: anchor.y - (anchor.y - current.y) * ratio,
      zoom,
    });
  }

  function finish(cancelled: boolean) {
    const current = gesture.current;
    if (!current) return;
    gesture.current = null;
    setInteracting(false);
    simulationRef.current?.alphaTarget(0);
    if (svg.current?.hasPointerCapture(current.pointerId))
      svg.current.releasePointerCapture(current.pointerId);
    // Pointer capture can retarget the browser's click to the SVG. Select using the original pressed node.
    if (!cancelled && current.node && !current.moved) onSelect(current.node.id);
  }

  useEffect(() => {
    finish(true);
    const ns: Positioned[] = data.nodes.map((n) => ({ ...n }));
    const ls: Edge[] = data.links.map((e) => ({ ...e }));
    live.current = ns;
    const simulation = forceSimulation<Positioned>(ns)
      .force(
        'link',
        forceLink<Positioned, Edge>(ls)
          .id((d) => d.id)
          .distance(115),
      )
      .force('charge', forceManyBody().strength(-230))
      .force('collide', forceCollide(35))
      .force('center', forceCenter(450, 240));
    simulationRef.current = simulation;
    setNodes([...ns]);
    setEdges([...ls]);
    simulation.on('tick', () => {
      setNodes([...ns]);
      setEdges([...ls]);
    });
    return () => {
      simulation.stop();
      simulationRef.current = null;
    };
  }, [data]);

  useEffect(() => {
    const element = svg.current;
    function wheel(event: WheelEvent) {
      const anchor = pointAt(event.clientX, event.clientY);
      if (!anchor) return;
      event.preventDefault();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 480 : 1);
      zoomAt(Math.exp(-Math.max(-150, Math.min(150, delta)) * 0.002), anchor);
    }
    element?.addEventListener('wheel', wheel, { passive: false });
    return () => element?.removeEventListener('wheel', wheel);
  }, []);

  function begin(event: React.PointerEvent<SVGElement>, node?: Positioned) {
    if (event.button !== 0 || event.isPrimary === false || gesture.current) return;
    const point = pointAt(event.clientX, event.clientY);
    if (!point) return;
    event.preventDefault();
    event.stopPropagation();
    const current = view.current;
    gesture.current = {
      pointerId: event.pointerId,
      node,
      startClient: { x: event.clientX, y: event.clientY },
      start: point,
      viewport: { ...current },
      moved: false,
      offset: node
        ? {
            x: (node.x || 0) - (point.x - current.x) / current.zoom,
            y: (node.y || 0) - (point.y - current.y) / current.zoom,
          }
        : { x: 0, y: 0 },
    };
    setInteracting(true);
    if (node) event.currentTarget.focus();
    svg.current?.setPointerCapture(event.pointerId);
  }

  function move(event: React.PointerEvent<SVGSVGElement>) {
    const current = gesture.current;
    if (!current || current.pointerId !== event.pointerId) return;
    const point = pointAt(event.clientX, event.clientY);
    if (!point) return;
    if (!current.moved) {
      if (
        Math.hypot(event.clientX - current.startClient.x, event.clientY - current.startClient.y) <
        DRAG_THRESHOLD
      )
        return;
      current.moved = true;
      if (current.node) simulationRef.current?.alphaTarget(0.15).restart();
    }
    if (current.node) {
      current.node.fx = current.node.x = (point.x - view.current.x) / view.current.zoom + current.offset.x;
      current.node.fy = current.node.y = (point.y - view.current.y) / view.current.zoom + current.offset.y;
      setNodes([...live.current]);
    } else {
      updateView({
        ...current.viewport,
        x: current.viewport.x + point.x - current.start.x,
        y: current.viewport.y + point.y - current.start.y,
      });
    }
  }
  const selectedNode = data.nodes.find((node) => node.id === selected);
  return (
    <div className={`graph-canvas${interacting ? ' is-interacting' : ''}`}>
      <div className="graph-tools">
        <button aria-label="缩小图谱" onClick={() => zoomAt(1 / 1.2)}>
          −
        </button>
        <button aria-label="重置图谱视图" title="重置平移与缩放" onClick={() => updateView(INITIAL_VIEW)}>
          {Math.round(viewport.zoom * 100)}% · 复位
        </button>
        <button aria-label="放大图谱" onClick={() => zoomAt(1.2)}>
          ＋
        </button>
        <span>拖动空白平移 · 滚轮缩放 · 点击节点查看证据</span>
      </div>
      <div className="graph-selection" role="status">
        {selectedNode
          ? `已选中：${selectedNode.name} · ${selectedNode.kind}，关系证据见图谱下方`
          : '点击节点查看实体和关系证据，也可拖动节点调整位置。'}
      </div>
      <svg
        ref={svg}
        viewBox="0 0 900 480"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="知识图谱力导向图"
        onPointerDown={(event) => begin(event)}
        onPointerMove={move}
        onPointerUp={(event) => {
          if (gesture.current?.pointerId !== event.pointerId) return;
          move(event);
          finish(false);
        }}
        onPointerCancel={(event) => {
          if (gesture.current?.pointerId === event.pointerId) finish(true);
        }}
        onLostPointerCapture={(event) => {
          if (gesture.current?.pointerId === event.pointerId) finish(true);
        }}
      >
        <defs>
          <marker
            id="graph-arrow"
            viewBox="0 0 10 10"
            refX="24"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#8ca79a" />
          </marker>
        </defs>
        <g
          data-testid="graph-viewport"
          transform={`translate(${viewport.x},${viewport.y}) scale(${viewport.zoom})`}
        >
          {edges.map((e) => {
            const a = e.source as Positioned,
              b = e.target as Positioned;
            return (
              <g key={e.id} pointerEvents="none">
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke="#b4c8bc"
                  strokeWidth="1.4"
                  markerEnd="url(#graph-arrow)"
                />
                <text
                  x={((a.x || 0) + (b.x || 0)) / 2}
                  y={((a.y || 0) + (b.y || 0)) / 2 - 5}
                  textAnchor="middle"
                  fontSize="10"
                  fill="#597e68"
                >
                  {e.predicate}
                </text>
              </g>
            );
          })}
          {nodes.map((n) => (
            <g
              key={n.id}
              transform={`translate(${n.x || 0},${n.y || 0})`}
              role="button"
              tabIndex={0}
              aria-label={`实体 ${n.name}`}
              aria-pressed={selected === n.id}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onSelect(n.id);
                }
              }}
              onClick={(e) => {
                if (e.detail === 0) onSelect(n.id);
              }}
              onPointerDown={(event) => begin(event, n)}
            >
              <circle r={selected === n.id ? 23 : 18} fill={selected === n.id ? '#b79040' : '#25735e'} />
              <text y="35" textAnchor="middle" fontSize="12" fill="#29483b">
                {n.name.length > 18 ? n.name.slice(0, 18) + '…' : n.name}
              </text>
              <title>
                {n.name} · {n.kind}
              </title>
            </g>
          ))}
        </g>
      </svg>
    </div>
  );
}
