// @vitest-environment jsdom
import { useState } from 'react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { GraphCanvas } from './GraphCanvas';
import type { GraphData } from './GraphCanvas';

const data: GraphData = {
  nodes: [
    { id: 'a', name: '研究团队', kind: '组织' },
    { id: 'b', name: '蛋白质A', kind: '概念' },
  ],
  links: [
    {
      id: 'edge',
      source: 'a',
      target: 'b',
      predicate: '发现',
      evidence: '发现证据',
      filename: 'a.md',
      document_id: 'doc',
      chunk_id: 'chunk',
      extraction: 'demo',
    },
  ],
};

beforeEach(() => {
  // jsdom has no PointerEvent/capture. Model the browser's pointer identity and retargeted release.
  class Pointer extends MouseEvent {
    pointerId: number;
    isPrimary: boolean;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 1;
      this.isPrimary = init.isPrimary ?? true;
    }
  }
  vi.stubGlobal('PointerEvent', Pointer);
  const captured = new WeakMap<Element, number>();
  Object.defineProperties(SVGElement.prototype, {
    setPointerCapture: {
      configurable: true,
      value(this: SVGElement, id: number) {
        captured.set(this, id);
      },
    },
    hasPointerCapture: {
      configurable: true,
      value(this: SVGElement, id: number) {
        return captured.get(this) === id;
      },
    },
    releasePointerCapture: {
      configurable: true,
      value(this: SVGElement) {
        captured.delete(this);
      },
    },
  });
  vi.spyOn(SVGElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 10,
    top: 20,
    width: 900,
    height: 480,
    right: 910,
    bottom: 500,
    x: 10,
    y: 20,
    toJSON() {},
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mount() {
  const select = vi.fn();
  function Graph() {
    const [selected, setSelected] = useState('');
    return (
      <GraphCanvas
        data={data}
        selected={selected}
        onSelect={(id) => {
          select(id);
          setSelected(id);
        }}
      />
    );
  }
  render(<Graph />);
  return {
    select,
    canvas: screen.getByRole('img', { name: '知识图谱力导向图' }),
    node: screen.getByRole('button', { name: '实体 研究团队' }),
  };
}
const point = (x: number, y: number, pointerId = 1) => ({ clientX: x, clientY: y, pointerId, button: 0 });
const viewport = () => screen.getByTestId('graph-viewport').getAttribute('transform');
function position(node: HTMLElement) {
  return node
    .getAttribute('transform')!
    .match(/-?[\d.]+(?:e[+-]?\d+)?/g)!
    .map(Number);
}

test('selects the pressed node when capture retargets pointerup and click to the SVG', () => {
  const { node, canvas, select } = mount();
  fireEvent.pointerDown(node, point(110, 120));
  fireEvent.pointerMove(canvas, point(111, 121));
  fireEvent.pointerUp(canvas, point(111, 121));
  fireEvent.click(canvas, { detail: 1 });
  expect(select).toHaveBeenCalledExactlyOnceWith('a');
  expect(node.getAttribute('aria-pressed')).toBe('true');
  expect(screen.getByRole('status').textContent).toContain('已选中：研究团队');
});

test('background drag pans the viewport, never selecting a node, and reset restores it', () => {
  const { canvas, select } = mount();
  fireEvent.pointerDown(canvas, point(110, 120));
  fireEvent.pointerMove(canvas, point(180, 160));
  fireEvent.pointerUp(canvas, point(180, 160));
  expect(viewport()).toBe('translate(70,40) scale(1)');
  expect(select).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '放大图谱' }));
  expect(viewport()).toContain('scale(1.2)');
  fireEvent.click(screen.getByRole('button', { name: '重置图谱视图' }));
  expect(viewport()).toBe('translate(0,0) scale(1)');
});

test('node dragging after pan and zoom preserves grab offset and never becomes a click', () => {
  const { canvas, node, select } = mount();
  fireEvent.pointerDown(canvas, point(110, 120));
  fireEvent.pointerMove(canvas, point(170, 150));
  fireEvent.pointerUp(canvas, point(170, 150));
  fireEvent.click(screen.getByRole('button', { name: '放大图谱' }));
  const beforeView = viewport(),
    [x, y] = position(node);
  fireEvent.pointerDown(node, point(300, 200));
  fireEvent.pointerMove(canvas, point(360, 236));
  fireEvent.pointerUp(canvas, point(360, 236));
  fireEvent.click(node, { detail: 1 });
  const [afterX, afterY] = position(node);
  expect(afterX - x).toBeCloseTo(50);
  expect(afterY - y).toBeCloseTo(30);
  expect(viewport()).toBe(beforeView);
  expect(select).not.toHaveBeenCalled();
});

test('accounts for a narrow SVG with vertical letterboxing when panning', () => {
  const { canvas } = mount();
  vi.mocked(SVGElement.prototype.getBoundingClientRect).mockReturnValue({
    left: 10,
    top: 20,
    width: 450,
    height: 300,
    right: 460,
    bottom: 320,
    x: 10,
    y: 20,
    toJSON() {},
  });
  fireEvent.pointerDown(canvas, point(100, 100));
  fireEvent.pointerMove(canvas, point(125, 115));
  fireEvent.pointerUp(canvas, point(125, 115));
  expect(viewport()).toBe('translate(50,30) scale(1)');
});

test('cancelled, lost and unrelated pointers do not leave the graph stuck or select nodes', () => {
  const { canvas, node, select } = mount();
  fireEvent.pointerDown(node, point(110, 120));
  fireEvent.pointerUp(canvas, point(110, 120, 2));
  expect(select).not.toHaveBeenCalled();
  fireEvent.pointerCancel(canvas, point(110, 120));
  fireEvent.pointerDown(node, point(110, 120));
  fireEvent.lostPointerCapture(canvas, { pointerId: 1 });
  fireEvent.pointerUp(canvas, point(110, 120));
  expect(select).not.toHaveBeenCalled();
  fireEvent.pointerDown(canvas, point(110, 120));
  fireEvent.pointerMove(canvas, point(160, 140));
  fireEvent.pointerUp(canvas, point(160, 140));
  expect(viewport()).toBe('translate(50,20) scale(1)');
  fireEvent.keyDown(node, { key: ' ' });
  expect(select).toHaveBeenCalledExactlyOnceWith('a');
});

test('wheel zoom is anchored at the pointer and cancels page scrolling', () => {
  const { canvas } = mount();
  const event = new WheelEvent('wheel', {
    clientX: 210,
    clientY: 120,
    deltaY: -100,
    bubbles: true,
    cancelable: true,
  });
  fireEvent(canvas, event);
  const [x, y, zoom] = viewport()!
    .match(/-?[\d.]+/g)!
    .map(Number);
  expect(zoom).toBeGreaterThan(1);
  expect((200 - x) / zoom).toBeCloseTo(200);
  expect((100 - y) / zoom).toBeCloseTo(100);
  expect(event.defaultPrevented).toBe(true);
});
